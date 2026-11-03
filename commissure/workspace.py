"""Workspace: one SQLite database, a sharded blob store, outputs, and a lockfile.

Layout of a workspace directory:

    state.db          SQLite in WAL mode; the schema below is its only definition
    blobs/ab/cd/<h>   content-addressed bytes, sharded by the first four hex chars
    outputs/          files rendered by stages, under outputs/<stage>/<run_id>/
    workspace.lock    held (flock, non-blocking) by whichever process is writing

`open_workspace` is the one schema initialization path. The CLI and the tests both go
through it; nothing else creates tables.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DB_NAME = "state.db"
BLOBS_DIR = "blobs"
OUTPUTS_DIR = "outputs"
LOCK_NAME = "workspace.lock"

# Each entry migrates the schema from version i to i + 1. Append only; never edit a
# migration that has shipped. Derived tables are rebuilt from originals rather than
# migrated in place, but their shape still lives here.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE sources (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,
        uri         TEXT NOT NULL,
        role        TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (kind, uri)
    );

    CREATE TABLE runs (
        id           INTEGER PRIMARY KEY,
        step         TEXT NOT NULL,
        trigger      TEXT NOT NULL,
        status       TEXT NOT NULL
                     CHECK (status IN ('running', 'succeeded', 'noop', 'failed')),
        source_id    INTEGER REFERENCES sources (id),
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        config       TEXT NOT NULL CHECK (json_valid(config)),
        error        TEXT,
        -- Volume signals. NULL means the step does not measure it, not zero.
        discovered   INTEGER,
        added        INTEGER,
        changed      INTEGER,
        unchanged    INTEGER,
        removed      INTEGER,
        -- Outcome signals, filled by stages that produce per-item outcomes.
        valid        INTEGER,
        warning      INTEGER,
        quarantined  INTEGER,
        skipped      INTEGER
    );

    -- Stable identity of (source, locator). Versions hang off it; edges point at it.
    CREATE TABLE items (
        id         INTEGER PRIMARY KEY,
        source_id  INTEGER NOT NULL REFERENCES sources (id),
        locator    TEXT NOT NULL,
        UNIQUE (source_id, locator)
    );

    CREATE TABLE item_versions (
        id              INTEGER PRIMARY KEY,
        item_id         INTEGER NOT NULL REFERENCES items (id),
        run_id          INTEGER NOT NULL REFERENCES runs (id),
        predecessor_id  INTEGER UNIQUE REFERENCES item_versions (id),
        bytes_hash      TEXT,
        text_hash       TEXT,
        size_bytes      INTEGER,
        role            TEXT NOT NULL,
        tombstone       INTEGER NOT NULL DEFAULT 0 CHECK (tombstone IN (0, 1)),
        is_current      INTEGER NOT NULL CHECK (is_current IN (0, 1)),
        created_at      TEXT NOT NULL,
        CHECK (tombstone = 0 OR (bytes_hash IS NULL AND size_bytes IS NULL))
    );
    CREATE UNIQUE INDEX item_versions_one_current
        ON item_versions (item_id) WHERE is_current = 1;
    CREATE INDEX item_versions_run ON item_versions (run_id);

    -- Shape only. Recipes decide what kinds mean.
    CREATE TABLE entities (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,
        attributes  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(attributes)),
        run_id      INTEGER NOT NULL REFERENCES runs (id),
        created_at  TEXT NOT NULL
    );

    -- Endpoints are an entity or an item (never an item version). An unresolved target
    -- keeps the raw reference text instead of pointing at a placeholder entity.
    CREATE TABLE edges (
        id                 INTEGER PRIMARY KEY,
        kind               TEXT NOT NULL,
        source_entity_id   INTEGER REFERENCES entities (id),
        source_item_id     INTEGER REFERENCES items (id),
        target_entity_id   INTEGER REFERENCES entities (id),
        target_item_id     INTEGER REFERENCES items (id),
        target_unresolved  TEXT,
        weight             REAL,
        evidence           TEXT,
        run_id             INTEGER NOT NULL REFERENCES runs (id),
        created_at         TEXT NOT NULL,
        CHECK ((source_entity_id IS NULL) + (source_item_id IS NULL) = 1),
        CHECK ((target_entity_id IS NOT NULL) + (target_item_id IS NOT NULL)
               + (target_unresolved IS NOT NULL) = 1)
    );

    -- Provenance back to the exact item versions that produced a row.
    CREATE TABLE entity_provenance (
        entity_id        INTEGER NOT NULL REFERENCES entities (id),
        item_version_id  INTEGER NOT NULL REFERENCES item_versions (id),
        PRIMARY KEY (entity_id, item_version_id)
    );
    CREATE TABLE edge_provenance (
        edge_id          INTEGER NOT NULL REFERENCES edges (id),
        item_version_id  INTEGER NOT NULL REFERENCES item_versions (id),
        PRIMARY KEY (edge_id, item_version_id)
    );

    CREATE TABLE extracted_text (
        id               INTEGER PRIMARY KEY,
        item_version_id  INTEGER NOT NULL REFERENCES item_versions (id),
        parser           TEXT NOT NULL,
        parser_version   TEXT NOT NULL,
        text             TEXT NOT NULL,
        text_hash        TEXT NOT NULL,
        run_id           INTEGER NOT NULL REFERENCES runs (id),
        created_at       TEXT NOT NULL,
        UNIQUE (item_version_id, parser, parser_version)
    );

    CREATE VIRTUAL TABLE extracted_text_fts USING fts5 (
        text, content = 'extracted_text', content_rowid = 'id'
    );
    CREATE TRIGGER extracted_text_fts_insert AFTER INSERT ON extracted_text BEGIN
        INSERT INTO extracted_text_fts (rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TRIGGER extracted_text_fts_delete AFTER DELETE ON extracted_text BEGIN
        INSERT INTO extracted_text_fts (extracted_text_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
    END;
    CREATE TRIGGER extracted_text_fts_update AFTER UPDATE ON extracted_text BEGIN
        INSERT INTO extracted_text_fts (extracted_text_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
        INSERT INTO extracted_text_fts (rowid, text) VALUES (new.id, new.text);
    END;

    -- The default view of items: current and not removed.
    CREATE VIEW current_items AS
    SELECT i.id AS item_id, i.source_id, s.kind AS source_kind, s.uri AS source_uri,
           i.locator, v.id AS item_version_id, v.bytes_hash, v.text_hash, v.size_bytes,
           v.role, v.run_id
    FROM items i
    JOIN item_versions v ON v.item_id = i.id AND v.is_current = 1 AND v.tombstone = 0
    JOIN sources s ON s.id = i.source_id;

    -- Most recent run per (step, source).
    CREATE VIEW latest_runs AS
    SELECT r.* FROM runs r
    WHERE r.id = (SELECT max(r2.id) FROM runs r2
                  WHERE r2.step = r.step AND r2.source_id IS r.source_id);

    -- One row per source change event. Unchanged locators produce no version row, so
    -- they never appear here; their count is on the run row.
    -- change_kind for 'changed' compares text_hash only under the same
    -- (parser, parser_version): 'content' if any pair differs, 'metadata_only' if
    -- every pair matches, 'unknown' if no pair exists yet.
    CREATE VIEW drift AS
    SELECT v.run_id, i.source_id, i.locator, v.id AS item_version_id,
           v.predecessor_id,
           CASE
               WHEN v.tombstone = 1 THEN 'removed'
               WHEN p.id IS NULL OR p.tombstone = 1 THEN 'added'
               ELSE 'changed'
           END AS change,
           CASE
               WHEN v.tombstone = 1 OR p.id IS NULL OR p.tombstone = 1 THEN NULL
               WHEN EXISTS (
                   SELECT 1 FROM extracted_text a JOIN extracted_text b
                     ON a.parser = b.parser AND a.parser_version = b.parser_version
                   WHERE a.item_version_id = v.id AND b.item_version_id = p.id
                     AND a.text_hash != b.text_hash) THEN 'content'
               WHEN EXISTS (
                   SELECT 1 FROM extracted_text a JOIN extracted_text b
                     ON a.parser = b.parser AND a.parser_version = b.parser_version
                   WHERE a.item_version_id = v.id AND b.item_version_id = p.id)
                   THEN 'metadata_only'
               ELSE 'unknown'
           END AS change_kind,
           p.bytes_hash AS previous_bytes_hash, v.bytes_hash
    FROM item_versions v
    JOIN items i ON i.id = v.item_id
    LEFT JOIN item_versions p ON p.id = v.predecessor_id;
    """,
    # 2: text_hash moves to extracted_text only (it is comparable only under one
    # parser and version); parsed structure; the per-item outcome ledger.
    """
    DROP VIEW current_items;
    ALTER TABLE item_versions DROP COLUMN text_hash;
    CREATE VIEW current_items AS
    SELECT i.id AS item_id, i.source_id, s.kind AS source_kind, s.uri AS source_uri,
           i.locator, v.id AS item_version_id, v.bytes_hash, v.size_bytes, v.role, v.run_id
    FROM items i
    JOIN item_versions v ON v.item_id = i.id AND v.is_current = 1 AND v.tombstone = 0
    JOIN sources s ON s.id = i.source_id;

    ALTER TABLE extracted_text
        ADD COLUMN structure TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(structure));

    -- One outcome per item a stage touched, keyed by the tool and version that ran.
    -- For stage 'parse', tool is the parser and tool_version its computed version.
    -- A row means "done under this key": reruns skip it, including quarantined and
    -- skipped items, so a corrupt file is not retried until the tool changes.
    CREATE TABLE item_outcomes (
        id                 INTEGER PRIMARY KEY,
        run_id             INTEGER NOT NULL REFERENCES runs (id),
        stage              TEXT NOT NULL,
        item_version_id    INTEGER NOT NULL REFERENCES item_versions (id),
        tool               TEXT NOT NULL,
        tool_version       TEXT NOT NULL,
        outcome            TEXT NOT NULL
                           CHECK (outcome IN ('valid', 'warning', 'quarantined', 'skipped')),
        reason_code        TEXT,
        diagnostics        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(diagnostics)),
        extracted_text_id  INTEGER REFERENCES extracted_text (id),
        created_at         TEXT NOT NULL,
        UNIQUE (stage, item_version_id, tool, tool_version),
        CHECK ((outcome = 'valid') = (reason_code IS NULL)),
        -- A result row exists exactly for usable outcomes; never a fabricated one.
        CHECK ((outcome IN ('valid', 'warning')) = (extracted_text_id IS NOT NULL))
    );
    CREATE INDEX item_outcomes_run ON item_outcomes (run_id);

    -- The latest text per (current item, parser). `stale` is 1 when the newest
    -- outcome for that parser came from a different version (it was quarantined or
    -- produced no row), so this row is an older fallback and must be shown as such.
    CREATE VIEW current_text AS
    SELECT ci.item_id, ci.source_id, ci.locator, ci.role, ci.item_version_id,
           et.id AS extracted_text_id, et.parser, et.parser_version, et.text_hash,
           et.text, et.structure, et.run_id,
           (SELECT o.tool_version FROM item_outcomes o
            WHERE o.stage = 'parse' AND o.item_version_id = et.item_version_id
              AND o.tool = et.parser
            ORDER BY o.id DESC LIMIT 1) IS NOT et.parser_version AS stale
    FROM current_items ci
    JOIN extracted_text et ON et.item_version_id = ci.item_version_id
    WHERE et.id = (SELECT max(e2.id) FROM extracted_text e2
                   WHERE e2.item_version_id = et.item_version_id AND e2.parser = et.parser);

    -- Everything that was not plainly valid, with where it came from.
    CREATE VIEW outcome_issues AS
    SELECT o.run_id, o.stage, i.source_id, i.locator, o.item_version_id, v.is_current,
           o.tool, o.tool_version, o.outcome, o.reason_code, o.diagnostics
    FROM item_outcomes o
    JOIN item_versions v ON v.id = o.item_version_id
    JOIN items i ON i.id = v.item_id
    WHERE o.outcome != 'valid';
    """,
    # 3: one general outcome ledger for every stage (parse and recipe stages), recipe
    # payloads, lineage, current stage fingerprints, and resume bookkeeping on runs.
    """
    CREATE TABLE records (
        id            INTEGER PRIMARY KEY,
        stage         TEXT NOT NULL,
        payload       TEXT NOT NULL CHECK (json_valid(payload)),
        payload_hash  TEXT NOT NULL,
        created_at    TEXT NOT NULL
    );

    -- One row per subject a stage run touched. Rows are never updated; the latest row
    -- per (stage, subject_key) is the current one. The fingerprints say how the row
    -- was produced; for recipe stages they are labels, not a cache key.
    CREATE TABLE outcomes (
        id                 INTEGER PRIMARY KEY,
        run_id             INTEGER NOT NULL REFERENCES runs (id),
        stage              TEXT NOT NULL,
        subject_kind       TEXT NOT NULL CHECK (subject_kind IN ('item', 'group', 'run')),
        subject_key        TEXT NOT NULL,
        item_version_id    INTEGER REFERENCES item_versions (id),
        input_fp           TEXT NOT NULL,
        stage_fp           TEXT NOT NULL,
        params             TEXT NOT NULL CHECK (json_valid(params)),
        outcome            TEXT NOT NULL
                           CHECK (outcome IN ('valid', 'warning', 'quarantined', 'skipped')),
        reason_code        TEXT,
        diagnostics        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(diagnostics)),
        extracted_text_id  INTEGER REFERENCES extracted_text (id),
        record_id          INTEGER REFERENCES records (id),
        created_at         TEXT NOT NULL,
        CHECK ((subject_kind = 'item') = (item_version_id IS NOT NULL)),
        CHECK ((outcome = 'valid') = (reason_code IS NULL)),
        CHECK (extracted_text_id IS NULL OR record_id IS NULL),
        -- A result row exists exactly for usable outcomes; never a fabricated one.
        CHECK ((outcome IN ('valid', 'warning'))
               = (extracted_text_id IS NOT NULL OR record_id IS NOT NULL))
    );
    CREATE INDEX outcomes_subject ON outcomes (stage, subject_key);
    CREATE INDEX outcomes_version ON outcomes (item_version_id);
    CREATE INDEX outcomes_run ON outcomes (run_id);

    -- Lineage: the upstream outcomes (parse text, earlier stages) a row consumed.
    CREATE TABLE outcome_inputs (
        outcome_id           INTEGER NOT NULL REFERENCES outcomes (id),
        upstream_outcome_id  INTEGER NOT NULL REFERENCES outcomes (id),
        PRIMARY KEY (outcome_id, upstream_outcome_id)
    );

    -- The current fingerprint of every recipe stage, refreshed whenever the recipe
    -- loads, so the views can label earlier outputs as produced by other code.
    CREATE TABLE stages (
        stage       TEXT PRIMARY KEY,
        stage_fp    TEXT NOT NULL,
        params      TEXT NOT NULL CHECK (json_valid(params)),
        recipe_fp   TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    ALTER TABLE runs ADD COLUMN stage_fp TEXT;
    ALTER TABLE runs ADD COLUMN recipe_fp TEXT;
    ALTER TABLE runs ADD COLUMN params TEXT;
    ALTER TABLE runs ADD COLUMN remaining INTEGER;  -- pending subjects left by --limit or a fault
    ALTER TABLE runs ADD COLUMN resumes_run_id INTEGER REFERENCES runs (id);
    ALTER TABLE runs ADD COLUMN override TEXT;  -- 'fresh' when the operator refused a resume

    INSERT INTO outcomes (id, run_id, stage, subject_kind, subject_key, item_version_id,
                          input_fp, stage_fp, params, outcome, reason_code, diagnostics,
                          extracted_text_id, created_at)
    SELECT o.id, o.run_id,
           CASE o.tool WHEN '' THEN 'parse' ELSE 'parse.' || o.tool END,
           'item', 'item:' || v.item_id, o.item_version_id, v.bytes_hash, o.tool_version,
           '{}', o.outcome, o.reason_code, o.diagnostics, o.extracted_text_id, o.created_at
    FROM item_outcomes o JOIN item_versions v ON v.id = o.item_version_id;

    DROP VIEW outcome_issues;
    DROP VIEW current_text;
    DROP TABLE item_outcomes;

    CREATE VIEW current_text AS
    SELECT ci.item_id, ci.source_id, ci.locator, ci.role, ci.item_version_id,
           et.id AS extracted_text_id, et.parser, et.parser_version, et.text_hash,
           et.text, et.structure, et.run_id,
           (SELECT o.stage_fp FROM outcomes o
            WHERE o.stage = 'parse.' || et.parser AND o.item_version_id = et.item_version_id
            ORDER BY o.id DESC LIMIT 1) IS NOT et.parser_version AS stale
    FROM current_items ci
    JOIN extracted_text et ON et.item_version_id = ci.item_version_id
    WHERE et.id = (SELECT max(e2.id) FROM extracted_text e2
                   WHERE e2.item_version_id = et.item_version_id AND e2.parser = et.parser);

    -- Latest seen: the newest outcome per (stage, subject) whose subject still exists,
    -- with every reason it may no longer describe the present.
    CREATE VIEW current_outcomes AS
    SELECT o.id AS outcome_id, o.run_id, o.stage, o.subject_kind, o.subject_key,
           o.item_version_id, o.outcome, o.reason_code, o.diagnostics, o.record_id,
           o.extracted_text_id, o.input_fp, o.stage_fp, o.params,
           CASE WHEN o.subject_kind = 'item'
                THEN o.item_version_id IS NOT ci.item_version_id ELSE 0 END AS version_stale,
           (s.stage_fp IS NOT NULL AND s.stage_fp != o.stage_fp) AS code_stale,
           (s.params IS NOT NULL AND s.params != o.params) AS params_stale,
           -- An input is stale when what it consumed is no longer what upstream has
           -- now: different content, or no usable result. A rerun that reproduces the
           -- same payload or text does not make downstream stale.
           EXISTS (SELECT 1 FROM outcome_inputs oi
                   JOIN outcomes u ON u.id = oi.upstream_outcome_id
                   JOIN outcomes now ON now.id = (
                       SELECT max(u2.id) FROM outcomes u2
                       WHERE u2.stage = u.stage AND u2.subject_key = u.subject_key)
                   LEFT JOIN records ur ON ur.id = u.record_id
                   LEFT JOIN records nr ON nr.id = now.record_id
                   LEFT JOIN extracted_text ue ON ue.id = u.extracted_text_id
                   LEFT JOIN extracted_text ne ON ne.id = now.extracted_text_id
                   WHERE oi.outcome_id = o.id AND now.id != u.id
                     AND (now.outcome IN ('quarantined', 'skipped')
                          OR coalesce(nr.payload_hash, ne.text_hash)
                             IS NOT coalesce(ur.payload_hash, ue.text_hash)))
               AS input_stale
    FROM outcomes o
    LEFT JOIN stages s ON s.stage = o.stage
    LEFT JOIN current_items ci
           ON o.subject_kind = 'item' AND ci.item_id = CAST(substr(o.subject_key, 6) AS INTEGER)
    WHERE o.id = (SELECT max(o2.id) FROM outcomes o2
                  WHERE o2.stage = o.stage AND o2.subject_key = o.subject_key)
      AND (o.subject_kind != 'item' OR ci.item_id IS NOT NULL);

    -- Latest valid: the newest payload per (stage, subject). `fallback` is 1 when the
    -- latest seen outcome produced no payload, so this one is older than the present.
    CREATE VIEW current_records AS
    SELECT v.stage, v.subject_kind, v.subject_key, v.item_version_id, v.id AS outcome_id,
           v.run_id, v.outcome, r.id AS record_id, r.payload, r.payload_hash,
           v.id != co.outcome_id AS fallback, co.outcome AS latest_outcome,
           co.version_stale, co.code_stale, co.params_stale, co.input_stale
    FROM outcomes v
    JOIN records r ON r.id = v.record_id
    JOIN current_outcomes co ON co.stage = v.stage AND co.subject_key = v.subject_key
    WHERE v.id = (SELECT max(v2.id) FROM outcomes v2
                  WHERE v2.stage = v.stage AND v2.subject_key = v.subject_key
                    AND v2.record_id IS NOT NULL);

    CREATE VIEW outcome_issues AS
    SELECT o.run_id, o.stage, o.subject_kind, o.subject_key, i.source_id, i.locator,
           o.item_version_id, v.is_current, o.stage_fp, o.outcome, o.reason_code,
           o.diagnostics
    FROM outcomes o
    LEFT JOIN item_versions v ON v.id = o.item_version_id
    LEFT JOIN items i ON i.id = v.item_id
    WHERE o.outcome != 'valid';
    """,
    # 4: ingest per-item quarantines (no item_version_id on stage 'ingest'), ignored
    # and attempted run signals, stage default_params tracking, and snapshot-scoped
    # current views for group and run subjects.
    """
    ALTER TABLE runs ADD COLUMN ignored INTEGER;
    ALTER TABLE runs ADD COLUMN attempted INTEGER;

    ALTER TABLE stages
        ADD COLUMN default_params TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(default_params));
    UPDATE stages SET default_params = params;

    DROP VIEW outcome_issues;
    DROP VIEW current_records;
    DROP VIEW current_outcomes;
    DROP VIEW current_text;

    CREATE TABLE _outcome_inputs_bak AS
        SELECT outcome_id, upstream_outcome_id FROM outcome_inputs;
    DROP TABLE outcome_inputs;

    CREATE TABLE outcomes_new (
        id                 INTEGER PRIMARY KEY,
        run_id             INTEGER NOT NULL REFERENCES runs (id),
        stage              TEXT NOT NULL,
        subject_kind       TEXT NOT NULL CHECK (subject_kind IN ('item', 'group', 'run')),
        subject_key        TEXT NOT NULL,
        item_version_id    INTEGER REFERENCES item_versions (id),
        input_fp           TEXT NOT NULL,
        stage_fp           TEXT NOT NULL,
        params             TEXT NOT NULL CHECK (json_valid(params)),
        outcome            TEXT NOT NULL
                           CHECK (outcome IN ('valid', 'warning', 'quarantined', 'skipped')),
        reason_code        TEXT,
        diagnostics        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(diagnostics)),
        extracted_text_id  INTEGER REFERENCES extracted_text (id),
        record_id          INTEGER REFERENCES records (id),
        created_at         TEXT NOT NULL,
        CHECK ((subject_kind = 'item' AND stage != 'ingest') = (item_version_id IS NOT NULL)),
        CHECK ((outcome = 'valid') = (reason_code IS NULL)),
        CHECK (extracted_text_id IS NULL OR record_id IS NULL),
        -- A result row exists exactly for usable outcomes; never a fabricated one.
        CHECK ((outcome IN ('valid', 'warning'))
               = (extracted_text_id IS NOT NULL OR record_id IS NOT NULL))
    );
    INSERT INTO outcomes_new SELECT * FROM outcomes;
    DROP TABLE outcomes;
    ALTER TABLE outcomes_new RENAME TO outcomes;
    CREATE INDEX outcomes_subject ON outcomes (stage, subject_key);
    CREATE INDEX outcomes_version ON outcomes (item_version_id);
    CREATE INDEX outcomes_run ON outcomes (run_id);

    CREATE TABLE outcome_inputs (
        outcome_id           INTEGER NOT NULL REFERENCES outcomes (id),
        upstream_outcome_id  INTEGER NOT NULL REFERENCES outcomes (id),
        PRIMARY KEY (outcome_id, upstream_outcome_id)
    );
    INSERT INTO outcome_inputs SELECT outcome_id, upstream_outcome_id FROM _outcome_inputs_bak;
    DROP TABLE _outcome_inputs_bak;

    CREATE VIEW current_text AS
    SELECT ci.item_id, ci.source_id, ci.locator, ci.role, ci.item_version_id,
           et.id AS extracted_text_id, et.parser, et.parser_version, et.text_hash,
           et.text, et.structure, et.run_id,
           (SELECT o.stage_fp FROM outcomes o
            WHERE o.stage = 'parse.' || et.parser AND o.item_version_id = et.item_version_id
            ORDER BY o.id DESC LIMIT 1) IS NOT et.parser_version AS stale
    FROM current_items ci
    JOIN extracted_text et ON et.item_version_id = ci.item_version_id
    WHERE et.id = (SELECT max(e2.id) FROM extracted_text e2
                   WHERE e2.item_version_id = et.item_version_id AND e2.parser = et.parser);

    -- Latest seen: for items, the newest outcome whose item still exists (or for
    -- stage 'ingest', an unreadable file from the source's latest ingest run); for
    -- group and run subjects, rows from the stage's latest completed snapshot.
    CREATE VIEW current_outcomes AS
    WITH RECURSIVE latest_snapshot(step, run_id) AS (
        SELECT r.step, max(r.id)
        FROM runs r
        WHERE r.remaining = 0
        GROUP BY r.step
        UNION ALL
        SELECT ls.step, r.resumes_run_id
        FROM latest_snapshot ls
        JOIN runs r ON r.id = ls.run_id
        WHERE r.resumes_run_id IS NOT NULL
    )
    SELECT o.id AS outcome_id, o.run_id, o.stage, o.subject_kind, o.subject_key,
           o.item_version_id, o.outcome, o.reason_code, o.diagnostics, o.record_id,
           o.extracted_text_id, o.input_fp, o.stage_fp, o.params,
           CASE WHEN o.subject_kind = 'item' AND o.stage != 'ingest'
                THEN o.item_version_id IS NOT ci.item_version_id ELSE 0 END AS version_stale,
           (s.stage_fp IS NOT NULL AND s.stage_fp != o.stage_fp) AS code_stale,
           (s.params IS NOT NULL AND s.params != o.params) AS params_stale,
           EXISTS (SELECT 1 FROM outcome_inputs oi
                   JOIN outcomes u ON u.id = oi.upstream_outcome_id
                   JOIN outcomes now ON now.id = (
                       SELECT max(u2.id) FROM outcomes u2
                       WHERE u2.stage = u.stage AND u2.subject_key = u.subject_key)
                   LEFT JOIN records ur ON ur.id = u.record_id
                   LEFT JOIN records nr ON nr.id = now.record_id
                   LEFT JOIN extracted_text ue ON ue.id = u.extracted_text_id
                   LEFT JOIN extracted_text ne ON ne.id = now.extracted_text_id
                   WHERE oi.outcome_id = o.id AND now.id != u.id
                     AND (now.outcome IN ('quarantined', 'skipped')
                          OR coalesce(nr.payload_hash, ne.text_hash)
                             IS NOT coalesce(ur.payload_hash, ue.text_hash)))
               AS input_stale
    FROM outcomes o
    LEFT JOIN stages s ON s.stage = o.stage
    LEFT JOIN current_items ci
           ON o.subject_kind = 'item' AND ci.item_id = CAST(substr(o.subject_key, 6) AS INTEGER)
    WHERE (
        (o.subject_kind = 'item' AND o.stage = 'ingest'
         AND o.run_id = (
             SELECT max(r.id) FROM runs r
             JOIN items i2 ON i2.source_id = r.source_id
             WHERE r.step = 'ingest'
               AND 'item:' || i2.id = o.subject_key
               AND (r.status IN ('succeeded', 'noop') OR r.quarantined > 0)
         ))
        OR (o.subject_kind = 'item' AND o.stage != 'ingest'
            AND ci.item_id IS NOT NULL
            AND o.id = (SELECT max(o2.id) FROM outcomes o2
                        WHERE o2.stage = o.stage AND o2.subject_key = o.subject_key))
        OR (o.subject_kind IN ('group', 'run')
            AND o.run_id IN (SELECT run_id FROM latest_snapshot WHERE step = o.stage)
            AND o.id = (SELECT max(o2.id) FROM outcomes o2
                        WHERE o2.stage = o.stage AND o2.subject_key = o.subject_key
                          AND o2.run_id IN (
                              SELECT run_id FROM latest_snapshot WHERE step = o.stage
                          )))
    );

    -- Latest valid: for items, the newest payload per (stage, subject), with
    -- `fallback = 1` when the latest seen outcome produced no payload; for group and
    -- run subjects, payloads from the latest completed snapshot.
    CREATE VIEW current_records AS
    SELECT v.stage, v.subject_kind, v.subject_key, v.item_version_id, v.id AS outcome_id,
           v.run_id, v.outcome, v.reason_code, v.diagnostics,
           r.id AS record_id, r.payload, r.payload_hash,
           v.id != co.outcome_id AS fallback, co.outcome AS latest_outcome,
           co.version_stale, co.code_stale, co.params_stale, co.input_stale
    FROM outcomes v
    JOIN records r ON r.id = v.record_id
    JOIN current_outcomes co ON co.stage = v.stage AND co.subject_key = v.subject_key
    WHERE v.id = CASE
        WHEN v.subject_kind = 'item' THEN (
            SELECT max(v2.id) FROM outcomes v2
            WHERE v2.stage = v.stage AND v2.subject_key = v.subject_key
              AND v2.record_id IS NOT NULL
        )
        ELSE co.outcome_id
    END;

    CREATE VIEW outcome_issues AS
    SELECT o.run_id, o.stage, o.subject_kind, o.subject_key, i.source_id, i.locator,
           o.item_version_id, v.is_current, o.stage_fp, o.outcome, o.reason_code,
           o.diagnostics
    FROM outcomes o
    LEFT JOIN item_versions v ON v.id = o.item_version_id
    LEFT JOIN items i ON i.id = coalesce(
        v.item_id,
        CASE WHEN o.subject_kind = 'item' AND substr(o.subject_key, 1, 5) = 'item:'
                  AND substr(o.subject_key, 6) GLOB '[0-9]*'
             THEN CAST(substr(o.subject_key, 6) AS INTEGER) END
    )
    WHERE o.outcome != 'valid';
    """,
    # 5: content-addressed LLM response cache (written before validation) and per-call
    # ledger with per-run and per-model summary views.
    """
    CREATE TABLE llm_cache (
        cache_key      TEXT PRIMARY KEY,
        provider       TEXT NOT NULL,
        model          TEXT NOT NULL,
        request_json   TEXT NOT NULL CHECK (json_valid(request_json)),
        raw_response   TEXT NOT NULL,
        input_tokens   INTEGER,
        output_tokens  INTEGER,
        cost_usd       REAL,
        created_at     TEXT NOT NULL
    );

    CREATE TABLE llm_calls (
        id             INTEGER PRIMARY KEY,
        run_id         INTEGER NOT NULL REFERENCES runs (id),
        stage          TEXT NOT NULL,
        subject_key    TEXT NOT NULL,
        provider       TEXT NOT NULL,
        model          TEXT NOT NULL,
        cache_key      TEXT NOT NULL,
        request_hash   TEXT NOT NULL,
        prompt_hash    TEXT NOT NULL,
        input_tokens   INTEGER,
        output_tokens  INTEGER,
        cost_usd       REAL,
        latency_ms     INTEGER NOT NULL,
        cached         INTEGER NOT NULL CHECK (cached IN (0, 1)),
        repairs_count  INTEGER NOT NULL DEFAULT 0,
        status         TEXT NOT NULL
                       CHECK (status IN ('ok', 'invalid', 'transport_error', 'refused')),
        error          TEXT,
        created_at     TEXT NOT NULL
    );
    CREATE INDEX llm_calls_run ON llm_calls (run_id);
    CREATE INDEX llm_calls_model ON llm_calls (provider, model);

    CREATE VIEW llm_calls_by_run AS
    SELECT run_id,
           stage,
           count(*) AS calls,
           sum(status = 'ok') AS ok_calls,
           sum(status = 'invalid') AS invalid_calls,
           sum(status = 'transport_error') AS transport_error_calls,
           sum(status = 'refused') AS refused_calls,
           sum(cached) AS cached_calls,
           sum(repairs_count) AS repairs_total,
           sum(input_tokens) AS input_tokens,
           sum(output_tokens) AS output_tokens,
           sum(cost_usd) AS cost_usd,
           CAST(sum(cached) AS REAL) / count(*) AS cache_hit_rate,
           CAST(sum(status = 'invalid') AS REAL) / count(*) AS invalid_rate,
           coalesce(sum(latency_ms), 0) AS latency_ms
    FROM llm_calls
    GROUP BY run_id, stage;

    CREATE VIEW llm_calls_by_model AS
    SELECT provider,
           model,
           count(*) AS calls,
           sum(status = 'ok') AS ok_calls,
           sum(status = 'invalid') AS invalid_calls,
           sum(status = 'transport_error') AS transport_error_calls,
           sum(status = 'refused') AS refused_calls,
           sum(cached) AS cached_calls,
           sum(repairs_count) AS repairs_total,
           sum(input_tokens) AS input_tokens,
           sum(output_tokens) AS output_tokens,
           sum(cost_usd) AS cost_usd,
           CAST(sum(cached) AS REAL) / count(*) AS cache_hit_rate,
           CAST(sum(status = 'invalid') AS REAL) / count(*) AS invalid_rate,
           coalesce(sum(latency_ms), 0) AS latency_ms
    FROM llm_calls
    GROUP BY provider, model;
    """,
    # 6: evidence (commissure/evidence.py). One row per pointer in a stored payload,
    # keyed by the record (a record belongs to exactly one outcome). Rows belong to their
    # run's snapshot and are never modified; the trigger makes that a refusal.
    """
    CREATE TABLE evidence (
        id                 INTEGER PRIMARY KEY,
        record_id          INTEGER NOT NULL REFERENCES records (id),
        pointer            TEXT NOT NULL,
        kind               TEXT NOT NULL,
        copied             INTEGER NOT NULL CHECK (copied IN (0, 1)),
        item_version_id    INTEGER NOT NULL REFERENCES item_versions (id),
        extracted_text_id  INTEGER REFERENCES extracted_text (id),
        span_start         INTEGER,
        span_end           INTEGER,
        page               INTEGER,
        via_ocr            INTEGER NOT NULL CHECK (via_ocr IN (0, 1)),
        confidence         REAL,
        CHECK ((span_start IS NULL) = (span_end IS NULL)),
        CHECK (span_start IS NULL
               OR (extracted_text_id IS NOT NULL AND 0 <= span_start AND span_start < span_end)),
        UNIQUE (record_id, pointer)
    );
    CREATE INDEX evidence_item_version ON evidence (item_version_id);
    CREATE TRIGGER evidence_never_modified BEFORE UPDATE ON evidence BEGIN
        SELECT RAISE(ABORT, 'evidence rows are never modified');
    END;

    -- What a reviewer checks, for current outcomes: evidence rows from records, plus page
    -- evidence read from parse diagnostics. Parse runs once per parser version, so pages
    -- recorded before evidence existed are covered here without a backfill.
    CREATE VIEW evidence_review AS
    SELECT co.outcome_id, co.run_id, co.stage, co.subject_kind, co.subject_key, co.outcome,
           'record' AS origin, e.id AS evidence_id, e.pointer, e.kind, e.copied,
           e.item_version_id, i.source_id, s.uri AS source_uri, i.locator, v.bytes_hash,
           e.extracted_text_id, e.span_start, e.span_end, e.page, e.via_ocr, e.confidence,
           CASE WHEN e.span_start IS NOT NULL
                THEN substr(t.text, e.span_start + 1, e.span_end - e.span_start) END
               AS span_text
    FROM current_outcomes co
    JOIN evidence e ON e.record_id = co.record_id
    JOIN item_versions v ON v.id = e.item_version_id
    JOIN items i ON i.id = v.item_id
    JOIN sources s ON s.id = i.source_id
    LEFT JOIN extracted_text t ON t.id = e.extracted_text_id
    UNION ALL
    SELECT co.outcome_id, co.run_id, co.stage, co.subject_kind, co.subject_key, co.outcome,
           'parse', NULL, NULL, json_extract(d.value, '$.code'), 0,
           co.item_version_id, i.source_id, s.uri, i.locator, v.bytes_hash,
           co.extracted_text_id, NULL, NULL, p.value,
           json_extract(d.value, '$.code') = 'ocr_pages',
           json_extract(d.value, '$.detail.confidence."' || p.value || '"'),
           NULL
    FROM current_outcomes co
    JOIN json_each(co.diagnostics) d
    JOIN json_each(json_extract(d.value, '$.detail.pages')) p
    JOIN item_versions v ON v.id = co.item_version_id
    JOIN items i ON i.id = v.item_id
    JOIN sources s ON s.id = i.source_id
    WHERE co.stage LIKE 'parse.%';
    """,
    # 7: the verdict log (commissure/review.py). Samples record what was drawn and how;
    # instances record what was judged (a sample's, or an ad hoc spot check when
    # sample_id is NULL); verdicts are human answers. All three are append-only: they are
    # originals, not derived data, and the latest verdict on an instance counts.
    """
    CREATE TABLE review_samples (
        id               INTEGER PRIMARY KEY,
        check_id         TEXT NOT NULL,
        question         TEXT NOT NULL,
        snapshot_run_id  INTEGER REFERENCES runs (id),
        seed             INTEGER NOT NULL,
        population       INTEGER NOT NULL CHECK (population > 0),
        n                INTEGER NOT NULL CHECK (n > 0 AND n <= population),
        reviewer         TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
        created_at       TEXT NOT NULL
    );
    CREATE TABLE review_instances (
        id           INTEGER PRIMARY KEY,
        sample_id    INTEGER REFERENCES review_samples (id),
        position     INTEGER NOT NULL,
        check_id     TEXT NOT NULL,
        outcome_id   INTEGER NOT NULL REFERENCES outcomes (id),
        stage        TEXT NOT NULL,
        subject_key  TEXT NOT NULL,
        pointer      TEXT,
        page         INTEGER,
        value_hash   TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        CHECK ((pointer IS NULL) != (page IS NULL)),
        UNIQUE (sample_id, position)
    );
    CREATE INDEX review_instances_match
        ON review_instances (check_id, subject_key, value_hash);
    CREATE TABLE verdicts (
        id           INTEGER PRIMARY KEY,
        instance_id  INTEGER NOT NULL REFERENCES review_instances (id),
        answer       TEXT NOT NULL CHECK (answer IN ('correct', 'wrong', 'unclear')),
        note         TEXT,
        reviewer     TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
        created_at   TEXT NOT NULL
    );
    CREATE INDEX verdicts_instance ON verdicts (instance_id);
    CREATE TRIGGER review_samples_append_only_u BEFORE UPDATE ON review_samples
        BEGIN SELECT RAISE(ABORT, 'review records are append-only'); END;
    CREATE TRIGGER review_samples_append_only_d BEFORE DELETE ON review_samples
        BEGIN SELECT RAISE(ABORT, 'review records are append-only'); END;
    CREATE TRIGGER review_instances_append_only_u BEFORE UPDATE ON review_instances
        BEGIN SELECT RAISE(ABORT, 'review records are append-only'); END;
    CREATE TRIGGER review_instances_append_only_d BEFORE DELETE ON review_instances
        BEGIN SELECT RAISE(ABORT, 'review records are append-only'); END;
    CREATE TRIGGER verdicts_append_only_u BEFORE UPDATE ON verdicts
        BEGIN SELECT RAISE(ABORT, 'review records are append-only'); END;
    CREATE TRIGGER verdicts_append_only_d BEFORE DELETE ON verdicts
        BEGIN SELECT RAISE(ABORT, 'review records are append-only'); END;
    """,
]


class WorkspaceError(Exception):
    """The workspace cannot be opened as requested."""


class WorkspaceLocked(WorkspaceError):
    """Another process holds the workspace lock."""


class SourceReadError(ValueError):
    """Reading or verifying a source file failed (not a workspace fault)."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hash_file(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


_CONN_LOCK = threading.RLock()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the pragmas every connection needs.

    Autocommit mode: transactions are explicit via `transaction()`.
    """
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    with _CONN_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def init_schema(conn: sqlite3.Connection) -> int:
    """Bring the database to the latest schema version and return that version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(MIGRATIONS):
        raise WorkspaceError(
            f"database schema version {current} is newer than this code "
            f"({len(MIGRATIONS)}); upgrade commissure"
        )
    for version in range(current, len(MIGRATIONS)):
        # executescript would commit a pending transaction first, so the transaction
        # and the version bump live inside the script and apply atomically.
        script = (
            f"BEGIN IMMEDIATE;\n{MIGRATIONS[version]}\n"
            f"PRAGMA user_version = {version + 1};\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)


@dataclass(frozen=True)
class BlobStore:
    """Content-addressed copies of ingested bytes, the record of exactly what was read.

    Blobs are read-only and verified while copied. There is no fsync and no garbage
    collection: a failed run can leave orphan blobs, which are harmless.
    """

    root: Path

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:4] / digest

    def has(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def put_file(self, src: Path, digest: str) -> Path:
        """Copy `src` into the store under `digest`, verifying the bytes match.

        Raises ValueError if the file no longer hashes to `digest` (it changed after it
        was hashed). Existing blobs are never rewritten.
        """
        dest = self.path_for(digest)
        if dest.is_file():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                try:
                    inp = src.open("rb")
                except FileNotFoundError as exc:
                    raise SourceReadError("vanished", f"{type(exc).__name__}: {exc}") from exc
                except PermissionError as exc:
                    raise SourceReadError(
                        "permission_denied", f"{type(exc).__name__}: {exc}"
                    ) from exc
                except OSError as exc:
                    raise SourceReadError("read_error", f"{type(exc).__name__}: {exc}") from exc
                with inp:
                    while True:
                        try:
                            chunk = inp.read(1 << 20)
                        except PermissionError as exc:
                            raise SourceReadError(
                                "permission_denied", f"{type(exc).__name__}: {exc}"
                            ) from exc
                        except OSError as exc:
                            raise SourceReadError(
                                "read_error", f"{type(exc).__name__}: {exc}"
                            ) from exc
                        if not chunk:
                            break
                        h.update(chunk)
                        out.write(chunk)
            if h.hexdigest() != digest:
                raise SourceReadError(
                    "changed_during_read",
                    f"{src} changed while being stored (expected {digest})",
                )
            tmp.chmod(0o444)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return dest


def check_root(root: Path) -> Path:
    """Resolve `root` and refuse anything that cannot be a workspace.

    A directory that exists, is not empty, and has no state.db is refused, so a
    mistyped path never scribbles into an unrelated folder.
    """
    root = root.resolve()
    if root.exists():
        if not root.is_dir():
            raise WorkspaceError(f"workspace path {root} is not a directory")
        unexpected = {p.name for p in root.iterdir()} - {LOCK_NAME}
        if unexpected and not (root / DB_NAME).exists():
            raise WorkspaceError(
                f"{root} is not empty and has no {DB_NAME}; refusing to use it as a workspace"
            )
    return root


@contextmanager
def lock(root: Path) -> Iterator[None]:
    """Hold the workspace lock for the duration, or raise WorkspaceLocked at once.

    flock is released by the OS when the holder exits, so a crashed run never leaves
    a stale lock. POSIX only.
    """
    root = check_root(root)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 200, 0).decode(errors="replace").strip()
            raise WorkspaceLocked(
                f"workspace {root} is locked by another process ({holder or 'unknown'})"
            ) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid={os.getpid()} since={utc_now()}\n".encode(), 0)
        yield
    finally:
        os.close(fd)


@dataclass
class Workspace:
    root: Path
    conn: sqlite3.Connection
    blobs: BlobStore
    outputs: Path

    def close(self) -> None:
        self.conn.close()


def open_workspace(root: Path) -> Workspace:
    """Open (creating if needed) the workspace at `root` and migrate its schema.

    The caller holds `lock(root)` for anything that writes, including a first open
    that creates or migrates the schema.
    """
    root = check_root(root)
    root.mkdir(parents=True, exist_ok=True)
    conn = connect(root / DB_NAME)
    try:
        init_schema(conn)
    except BaseException:
        conn.close()
        raise
    blobs = BlobStore(root / BLOBS_DIR)
    blobs.root.mkdir(exist_ok=True)
    outputs = root / OUTPUTS_DIR
    outputs.mkdir(exist_ok=True)
    return Workspace(root=root, conn=conn, blobs=blobs, outputs=outputs)
