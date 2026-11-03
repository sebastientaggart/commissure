"""The schema as production builds it, via open_workspace."""

import sqlite3

import pytest

from commissure.workspace import (
    DB_NAME,
    MIGRATIONS,
    WorkspaceError,
    connect,
    init_schema,
    open_workspace,
    transaction,
    utc_now,
)

TABLES = {
    "sources", "runs", "items", "item_versions", "entities", "edges",
    "entity_provenance", "edge_provenance", "extracted_text", "extracted_text_fts",
    "records", "outcomes", "outcome_inputs", "stages",
    "llm_cache", "llm_calls", "evidence",
    "review_samples", "review_instances", "verdicts",
}  # fmt: skip
VIEWS = {
    "current_items", "latest_runs", "drift",
    "current_text", "current_outcomes", "current_records", "outcome_issues",
    "llm_calls_by_run", "llm_calls_by_model", "evidence_review",
}  # fmt: skip


def _names(conn, kind):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def test_open_creates_schema_in_wal_mode(ws):
    tables = {n for n in _names(ws.conn, "table") if not n.startswith("extracted_text_fts_")}
    assert tables == TABLES
    assert _names(ws.conn, "view") == VIEWS
    run_cols = {r["name"] for r in ws.conn.execute("PRAGMA table_info(runs)")}
    assert {"ignored", "attempted"} <= run_cols
    assert ws.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert ws.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert ws.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert ws.blobs.root.is_dir()
    assert ws.outputs == ws.root / "outputs" and ws.outputs.is_dir()


def test_reopen_is_idempotent(ws_root):
    open_workspace(ws_root).close()
    ws = open_workspace(ws_root)
    assert ws.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    ws.close()


def test_newer_schema_is_refused(ws_root):
    open_workspace(ws_root).close()
    conn = connect(ws_root / DB_NAME)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    with pytest.raises(WorkspaceError, match="newer than this code"):
        init_schema(conn)
    conn.close()


def test_unrelated_nonempty_folder_is_refused(tmp_path):
    (tmp_path / "notes.txt").write_text("not a workspace")
    with pytest.raises(WorkspaceError, match="refusing"):
        open_workspace(tmp_path)


def _run(conn):
    return conn.execute(
        "INSERT INTO runs (step, trigger, status, started_at, config)"
        " VALUES ('test', 'manual', 'running', ?, '{}')",
        (utc_now(),),
    ).lastrowid


def _item(conn, run_id):
    source = conn.execute(
        "INSERT INTO sources (kind, uri, role, created_at) VALUES ('folder', '/x', 'corpus', ?)",
        (utc_now(),),
    ).lastrowid
    item = conn.execute(
        "INSERT INTO items (source_id, locator) VALUES (?, 'a.txt')", (source,)
    ).lastrowid
    version = conn.execute(
        "INSERT INTO item_versions (item_id, run_id, bytes_hash, size_bytes, role,"
        " is_current, created_at) VALUES (?, ?, 'h1', 1, 'corpus', 1, ?)",
        (item, run_id, utc_now()),
    ).lastrowid
    return item, version


def test_only_one_current_version_per_item(ws):
    with transaction(ws.conn):
        run_id = _run(ws.conn)
        item, _ = _item(ws.conn, run_id)
    with pytest.raises(sqlite3.IntegrityError), transaction(ws.conn):
        ws.conn.execute(
            "INSERT INTO item_versions (item_id, run_id, bytes_hash, size_bytes, role,"
            " is_current, created_at) VALUES (?, ?, 'h2', 1, 'corpus', 1, ?)",
            (item, run_id, utc_now()),
        )


def test_fts_tracks_extracted_text(ws):
    conn = ws.conn
    with transaction(conn):
        run_id = _run(conn)
        _, version = _item(conn, run_id)
        text_id = conn.execute(
            "INSERT INTO extracted_text (item_version_id, parser, parser_version, text,"
            " text_hash, run_id, created_at) VALUES (?, 'p', '1', ?, 't', ?, ?)",
            (version, "the quick brown fox", run_id, utc_now()),
        ).lastrowid

    def hits(query):
        return [r[0] for r in conn.execute(
            "SELECT rowid FROM extracted_text_fts WHERE extracted_text_fts MATCH ?", (query,)
        )]  # fmt: skip

    assert hits('"quick brown"') == [text_id]
    with transaction(conn):
        conn.execute("DELETE FROM extracted_text WHERE id = ?", (text_id,))
    assert hits("quick") == []


def test_edge_target_is_exactly_one_of_entity_item_or_unresolved(ws):
    conn = ws.conn
    with transaction(conn):
        run_id = _run(conn)
        item, _ = _item(conn, run_id)
        entity = conn.execute(
            "INSERT INTO entities (kind, run_id, created_at) VALUES ('thing', ?, ?)",
            (run_id, utc_now()),
        ).lastrowid
        # An unresolved target stays unresolved: raw text, no placeholder entity.
        conn.execute(
            "INSERT INTO edges (kind, source_item_id, target_unresolved, run_id, created_at)"
            " VALUES ('refers_to', ?, 'ref-123', ?, ?)",
            (item, run_id, utc_now()),
        )
    with pytest.raises(sqlite3.IntegrityError), transaction(conn):
        conn.execute(
            "INSERT INTO edges (kind, source_item_id, target_entity_id, target_unresolved,"
            " run_id, created_at) VALUES ('refers_to', ?, ?, 'ref-123', ?, ?)",
            (item, entity, run_id, utc_now()),
        )
