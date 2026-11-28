"""Ingest: discover a source, classify every locator, and version what changed.

One source kind exists so far: a local folder. Every regular file under it, recursively,
is an item whose locator is its raw POSIX path relative to the folder (no Unicode
normalization or case folding), except paths matching the recorded ignore list
(`DEFAULT_IGNORE` unless overridden). Symlinks are not followed and are not items. Other
dotfiles are items; further filtering is a recipe decision.

Classification against the current version of each locator:

    added      no current version, or the current version is a tombstone
    changed    current version exists and its bytes_hash differs
    unchanged  bytes_hash matches; no row is written
    removed    current version exists but the locator was not discovered; a tombstone
               version is written so drift reports can name it

Unreadable files (permission denied, vanished, locked, changed mid-copy) are recorded as
per-item `quarantined` outcomes without blocking readable items or tombstoning prior
valid versions. Source files are only read. All version writes for readable items commit
in one transaction.
"""

from __future__ import annotations

import fnmatch
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from commissure.limits import DEFAULT_LIMITS, LimitExceeded, Limits
from commissure.runner import AllQuarantined, RunCancelled, canonical
from commissure.runs import EmptyDiscovery, finish_run, start_run
from commissure.workspace import (
    SourceReadError,
    Workspace,
    format_folder_uri,
    hash_file,
    resolve_source_uri,
    transaction,
    utc_now,
)

FOLDER = "folder"
DEFAULT_IGNORE: tuple[str, ...] = (
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "~$*",
    ".~lock.*#",
)


@dataclass(frozen=True)
class Discovered:
    path: Path
    bytes_hash: str
    size_bytes: int


@dataclass
class IngestResult:
    run_id: int
    status: str
    source_id: int
    discovered: int = 0
    ignored: int = 0
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    quarantined: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("warnings", None)
        return out


def effective_ignore(
    ignore: Sequence[str] | None = None, *, no_default_ignore: bool = False
) -> tuple[str, ...]:
    base = () if no_default_ignore else DEFAULT_IGNORE
    return (*base, *(ignore or ()))


def _is_ignored(locator: str, name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(locator, p) for p in patterns)


def walk_folder(
    folder: Path,
    ignore: Sequence[str] = DEFAULT_IGNORE,
    *,
    max_files: int | None = None,
) -> tuple[list[tuple[str, Path, int]], list[tuple[str, str, str]], int]:
    """Walk `folder` in stable order.

    Returns `(regular_files, stat_errors, ignored_count)` where `regular_files` holds
    `(locator, path, size_bytes)` and `stat_errors` holds `(locator, reason_code, message)`.
    Raises `LimitExceeded('too_many_source_files', ...)` as soon as discovered items
    exceed `max_files`.
    """
    files: list[tuple[str, Path, int]] = []
    errors: list[tuple[str, str, str]] = []
    ignored = 0
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            locator = path.relative_to(folder).as_posix()
            try:
                st = os.lstat(path)
            except FileNotFoundError as exc:
                if _is_ignored(locator, name, ignore):
                    ignored += 1
                else:
                    errors.append((locator, "vanished", f"{type(exc).__name__}: {exc}"))
            except PermissionError as exc:
                if _is_ignored(locator, name, ignore):
                    ignored += 1
                else:
                    errors.append((locator, "permission_denied", f"{type(exc).__name__}: {exc}"))
            except OSError as exc:
                if _is_ignored(locator, name, ignore):
                    ignored += 1
                else:
                    errors.append((locator, "read_error", f"{type(exc).__name__}: {exc}"))
            else:
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    continue
                if _is_ignored(locator, name, ignore):
                    ignored += 1
                    continue
                files.append((locator, path, st.st_size))
            discovered = len(files) + len(errors)
            if max_files is not None and discovered > max_files:
                raise LimitExceeded(
                    "too_many_source_files",
                    f"source folder {folder} exceeds max_source_files ({max_files})",
                    actual=discovered,
                    limit=max_files,
                )
    return files, errors, ignored


def iter_folder_files(
    folder: Path, ignore: Sequence[str] = DEFAULT_IGNORE
) -> Iterator[tuple[str, Path]]:
    """Yield (locator, path) for every non-ignored regular file in `folder`."""
    files, _, _ = walk_folder(folder, ignore)
    for locator, path, _ in files:
        yield locator, path


def discover_folder(folder: Path, ignore: Sequence[str] = DEFAULT_IGNORE) -> dict[str, Discovered]:
    # Every file is re-hashed on every ingest. An mtime or size shortcut would miss edits
    # that preserve both; revisit only if hashing unchanged files dominates wall time.
    return {
        locator: Discovered(path, hash_file(path), path.stat().st_size)
        for locator, path in iter_folder_files(folder, ignore)
    }


def check_folder_source(folder: Path, ws_root: Path) -> Path:
    """Resolve `folder` and refuse it before anything is created on disk.

    The workspace may not live inside the source, or ingest would read its own state
    and the workspace would write into an original input folder.
    """
    folder = folder.resolve()
    if not folder.is_dir():
        raise ValueError(f"source folder {folder} does not exist or is not a directory")
    if ws_root.resolve().is_relative_to(folder):
        raise ValueError(f"workspace {ws_root.resolve()} is inside source folder {folder}")
    return folder


def register_source(conn: sqlite3.Connection, kind: str, uri: str, role: str) -> int:
    """Return the id of active source (kind, uri), creating it if new.

    A source keeps the role it was registered with; asking for a different one is an
    error rather than a silent re-labelling of existing items. Retired sources are
    ignored so a re-ingest registers a fresh active source row.
    """
    row = conn.execute(
        "SELECT id, role FROM sources WHERE kind = ? AND uri = ? AND retired_at IS NULL",
        (kind, uri),
    ).fetchone()
    if row is not None:
        if row["role"] != role:
            raise ValueError(f"source {uri} is registered with role {row['role']!r}, not {role!r}")
        return row["id"]
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sources (kind, uri, role, created_at) VALUES (?, ?, ?, ?)",
            (kind, uri, role, utc_now()),
        )
    return cur.lastrowid


def _adopt_absolute_source(conn: sqlite3.Connection, folder: Path, stored_uri: str) -> None:
    """Rewrite an active absolute row for this same folder to its stored form, keeping its id.

    Workspaces written before folder sources were stored relative hold the resolved absolute
    path. Without this, the first ingest after upgrading misses that row and registers a second
    source for the same, unmoved folder, duplicating every item. The path still resolves to
    this folder, so rewriting it is safe; a moved folder's stale row is `relocate`'s job.
    """
    absolute = str(folder)
    if stored_uri == absolute:
        return
    rows = {
        r["uri"]: r["id"]
        for r in conn.execute(
            "SELECT id, uri FROM sources WHERE kind = ? AND uri IN (?, ?) AND retired_at IS NULL",
            (FOLDER, absolute, stored_uri),
        )
    }
    if absolute in rows and stored_uri not in rows:
        with transaction(conn):
            conn.execute("UPDATE sources SET uri = ? WHERE id = ?", (stored_uri, rows[absolute]))


def _stale_folder_warnings(
    conn: sqlite3.Connection,
    ws_root: Path,
    folder: Path,
    stored_uri: str,
    role: str,
) -> list[str]:
    """Warn when registering a new folder source while a same-role, same-name source is missing."""
    existing = conn.execute(
        "SELECT 1 FROM sources WHERE kind = ? AND uri = ? AND retired_at IS NULL",
        (FOLDER, stored_uri),
    ).fetchone()
    if existing is not None:
        return []
    warnings: list[str] = []
    for row in conn.execute(
        "SELECT id, uri FROM sources WHERE kind = ? AND role = ? AND retired_at IS NULL"
        " ORDER BY id",
        (FOLDER, role),
    ):
        resolved_path = Path(resolve_source_uri(row["uri"], ws_root, FOLDER))
        if resolved_path.name == folder.name and not resolved_path.exists():
            warnings.append(
                f"existing source #{row['id']} ({row['uri']}) with role {role!r} and folder"
                f" name {folder.name!r} no longer exists; if the folder moved, repair the"
                " workspace with `commissure sources relocate <old> <new>`"
            )
    return warnings


def _source_counts(conn: sqlite3.Connection, source_id: int) -> dict[str, int]:
    """Count items, lineage outcomes, review instances, and verdicts for `source_id`.

    Walks `outcome_inputs` in Python from one read of its edges because `outcome_inputs`
    is indexed by `outcome_id`, not `upstream_outcome_id`. Shared downstream outcomes
    (group and run outcomes fed by multiple sources) count for every source that feeds them.
    """
    items = conn.execute("SELECT count(*) FROM items WHERE source_id = ?", (source_id,)).fetchone()[
        0
    ]
    direct = {
        row[0]
        for row in conn.execute(
            "SELECT o.id FROM outcomes o "
            "LEFT JOIN item_versions v ON v.id = o.item_version_id "
            "JOIN items i ON i.id = coalesce("
            "    v.item_id,"
            "    CASE WHEN o.subject_kind = 'item' AND substr(o.subject_key, 1, 5) = 'item:'"
            "              AND substr(o.subject_key, 6) GLOB '[0-9]*'"
            "         THEN CAST(substr(o.subject_key, 6) AS INTEGER) END"
            ") WHERE i.source_id = ?",
            (source_id,),
        )
    }
    if not direct:
        return {"items": items, "outcomes": 0, "verdict_instances": 0, "verdicts": 0}
    downstream: dict[int, list[int]] = {}
    for out_id, up_id in conn.execute("SELECT outcome_id, upstream_outcome_id FROM outcome_inputs"):
        downstream.setdefault(up_id, []).append(out_id)
    reached = set(direct)
    stack = list(direct)
    while stack:
        cur = stack.pop()
        for nxt in downstream.get(cur, ()):
            if nxt not in reached:
                reached.add(nxt)
                stack.append(nxt)
    instance_ids = {
        row[0]
        for row in conn.execute("SELECT id, outcome_id FROM review_instances")
        if row[1] in reached
    }
    verdicts = (
        sum(
            1
            for (inst_id,) in conn.execute("SELECT instance_id FROM verdicts")
            if inst_id in instance_ids
        )
        if instance_ids
        else 0
    )
    return {
        "items": items,
        "outcomes": len(reached),
        "verdict_instances": len(instance_ids),
        "verdicts": verdicts,
    }


def _match_old_prefix(uri: str, ws_root: Path, old_prefixes: tuple[Path, ...]) -> Path | None:
    resolved_str = resolve_source_uri(uri, ws_root, FOLDER)
    candidates: list[Path] = [Path(resolved_str), Path(resolved_str).resolve()]
    raw = Path(uri)
    if raw.is_absolute():
        candidates.extend([raw, raw.resolve()])
    for cand in candidates:
        for prefix in old_prefixes:
            if cand.is_relative_to(prefix):
                return cand.relative_to(prefix)
    return None


def relocate_sources(
    ws: Workspace,
    old: Path,
    new: Path,
    *,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Rewrite active folder sources at or under `old` to `new`, keeping source ids."""
    new_expanded = new.expanduser()
    new_resolved = new_expanded.resolve()
    if not new_resolved.is_dir():
        raise ValueError(f"new folder {new} does not exist or is not a directory")

    old_expanded = old.expanduser()
    old_prefixes = tuple(
        dict.fromkeys((old_expanded.resolve(), Path(os.path.abspath(old_expanded))))
    )

    conn = ws.conn
    rows = conn.execute(
        "SELECT id, kind, uri, role FROM sources WHERE kind = ? AND retired_at IS NULL ORDER BY id",
        (FOLDER,),
    ).fetchall()

    matched: list[tuple[sqlite3.Row, Path]] = []
    for row in rows:
        rel = _match_old_prefix(row["uri"], ws.root, old_prefixes)
        if rel is not None:
            matched.append((row, rel))

    if not matched:
        raise ValueError(f"no folder source in {ws.root} is at or under {old}")

    relocated: list[dict[str, Any]] = []
    for row, rel in matched:
        target_folder = check_folder_source(new_resolved / rel, ws.root)
        new_uri = format_folder_uri(target_folder, ws.root)
        for other in rows:
            if other["id"] == row["id"]:
                continue
            other_resolved = Path(resolve_source_uri(other["uri"], ws.root, FOLDER)).resolve()
            if other["uri"] == new_uri or other_resolved == target_folder:
                c1 = _source_counts(conn, row["id"])
                c2 = _source_counts(conn, other["id"])
                raise ValueError(
                    f"relocating source #{row['id']} ({row['uri']} -> {new_uri}) collides with"
                    f" existing source #{other['id']} ({other['uri']});"
                    f" source #{row['id']} ({row['uri']}) has {c1['items']} item(s),"
                    f" {c1['outcomes']} outcome(s), {c1['verdict_instances']} verdict instance(s),"
                    f" {c1['verdicts']} verdict(s);"
                    f" source #{other['id']} ({other['uri']}) has {c2['items']} item(s),"
                    f" {c2['outcomes']} outcome(s), {c2['verdict_instances']} verdict instance(s),"
                    f" {c2['verdicts']} verdict(s)"
                    " (lineage counts include downstream group and run outcomes; a shared"
                    " downstream outcome counts for every source that feeds it)."
                    " To resolve, run `commissure sources retire <source-id>` on the unreviewed"
                    " source, then rerun `commissure sources relocate`"
                )
        relocated.append({"id": row["id"], "old_uri": row["uri"], "new_uri": new_uri})

    if not dry_run:
        with transaction(conn):
            for entry in relocated:
                conn.execute(
                    "UPDATE sources SET uri = ? WHERE id = ?",
                    (entry["new_uri"], entry["id"]),
                )
    return relocated


def retire_source(
    ws: Workspace,
    source_id: int,
    *,
    dry_run: bool = False,
    trigger: str = "manual",
) -> dict[str, Any]:
    """Tombstone every current item of `source_id` in one run and mark the source retired."""
    conn = ws.conn
    row = conn.execute(
        "SELECT id, kind, uri, role, retired_at FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no source #{source_id} in {ws.root}")
    if row["retired_at"] is not None:
        raise ValueError(f"source #{source_id} ({row['uri']}) is already retired")

    counts = _source_counts(conn, source_id)
    if counts["verdict_instances"] > 0:
        raise ValueError(
            f"cannot retire source #{source_id} ({row['uri']}): it has"
            f" {counts['verdict_instances']} verdict instance(s) and {counts['verdicts']}"
            " verdict(s) in its lineage (shared downstream outcomes count for every source"
            " that feeds them)"
        )

    current_rows = conn.execute(
        "SELECT i.id AS item_id, v.id AS version_id, v.role"
        " FROM items i"
        " JOIN item_versions v ON v.item_id = i.id AND v.is_current = 1 AND v.tombstone = 0"
        " WHERE i.source_id = ? ORDER BY i.id",
        (source_id,),
    ).fetchall()

    run_id: int | None = None
    if not dry_run:
        now = utc_now()
        status = "succeeded" if current_rows else "noop"
        config_json = canonical({"source_id": source_id})
        with transaction(conn):
            run_id = conn.execute(
                "INSERT INTO runs (step, trigger, status, source_id, started_at, finished_at,"
                " config, discovered, added, changed, unchanged, removed)"
                " VALUES ('retire', ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?)",
                (
                    trigger,
                    status,
                    source_id,
                    now,
                    now,
                    config_json,
                    len(current_rows),
                    len(current_rows),
                ),
            ).lastrowid
            for cur in current_rows:
                _retire(conn, cur["version_id"])
                conn.execute(
                    "INSERT INTO item_versions (item_id, run_id, predecessor_id, role,"
                    " tombstone, is_current, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)",
                    (cur["item_id"], run_id, cur["version_id"], cur["role"], now),
                )
            conn.execute("UPDATE sources SET retired_at = ? WHERE id = ?", (now, source_id))

    return {
        "source_id": source_id,
        "uri": row["uri"],
        "role": row["role"],
        "run_id": run_id,
        "tombstoned": len(current_rows),
        "counts": counts,
        "note": (
            "lineage counts include downstream group and run outcomes; a shared downstream"
            " outcome counts for every source that feeds it"
        ),
    }


def ingest_folder(
    ws: Workspace,
    folder: Path,
    *,
    role: str,
    trigger: str,
    config: dict[str, Any],
    allow_empty: bool = False,
    ignore: Sequence[str] = DEFAULT_IGNORE,
    limits: Limits = DEFAULT_LIMITS,
    cancel_event: threading.Event | None = None,
) -> IngestResult:
    """Run one ingest of `folder` into `ws`. The caller holds the workspace lock.

    Invalid arguments raise ValueError before anything is written. Unreadable or oversized
    source files get per-item `quarantined` outcomes while readable files commit together.
    The run fails only when nothing was ingestable (`EmptyDiscovery` or
    `AllQuarantined`), a source-wide limit was exceeded, or on an infrastructure fault;
    interrupts propagate after the run row is marked failed.
    """
    conn = ws.conn
    folder = check_folder_source(folder, ws.root)
    stored_uri = format_folder_uri(folder, ws.root)
    _adopt_absolute_source(conn, folder, stored_uri)
    warnings = _stale_folder_warnings(conn, ws.root, folder, stored_uri, role)
    source_id = register_source(conn, FOLDER, stored_uri, role)
    run_id = start_run(conn, "ingest", trigger=trigger, config=config, source_id=source_id)
    result = IngestResult(run_id=run_id, status="running", source_id=source_id, warnings=warnings)
    try:
        _ingest(
            ws,
            folder,
            source_id,
            role,
            allow_empty,
            ignore,
            result,
            limits=limits,
            cancel_event=cancel_event,
        )
    except BaseException as exc:  # Ctrl-C still leaves an honest ledger row
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        with transaction(conn):
            finish_run(
                conn,
                run_id,
                "failed",
                result.error,
                discovered=result.discovered,
                ignored=result.ignored,
                quarantined=result.quarantined,
            )
        if not isinstance(exc, Exception):
            raise
    return result


def _ingest(
    ws: Workspace,
    folder: Path,
    source_id: int,
    role: str,
    allow_empty: bool,
    ignore: Sequence[str],
    result: IngestResult,
    *,
    limits: Limits = DEFAULT_LIMITS,
    cancel_event: threading.Event | None = None,
) -> None:
    conn = ws.conn
    files, stat_errors, ignored = walk_folder(folder, ignore, max_files=limits.max_source_files)
    ingestable_bytes = sum(sz for _, _, sz in files if sz <= limits.max_file_bytes)
    if ingestable_bytes > limits.max_source_bytes:
        raise LimitExceeded(
            "source_bytes_too_large",
            f"source folder {folder} totals {ingestable_bytes} bytes,"
            f" exceeding max_source_bytes ({limits.max_source_bytes})",
            actual=ingestable_bytes,
            limit=limits.max_source_bytes,
        )
    found: dict[str, Discovered] = {}
    quarantined: dict[str, tuple[str, str, dict[str, Any] | None]] = {
        loc: (code, msg, None) for loc, code, msg in stat_errors
    }
    for locator, path, size_bytes in files:
        if cancel_event is not None and cancel_event.is_set():
            raise RunCancelled("interrupted by shutdown")
        if size_bytes > limits.max_file_bytes:
            quarantined[locator] = (
                "file_too_large",
                f"file size {size_bytes} bytes exceeds max_file_bytes ({limits.max_file_bytes})",
                {"actual": size_bytes, "limit": limits.max_file_bytes},
            )
            continue
        try:
            digest = hash_file(path)
        except FileNotFoundError as exc:
            quarantined[locator] = ("vanished", f"{type(exc).__name__}: {exc}", None)
        except PermissionError as exc:
            quarantined[locator] = ("permission_denied", f"{type(exc).__name__}: {exc}", None)
        except OSError as exc:
            quarantined[locator] = ("read_error", f"{type(exc).__name__}: {exc}", None)
        else:
            found[locator] = Discovered(path, digest, size_bytes)

    result.ignored = ignored
    result.discovered = len(found) + len(quarantined)
    if result.discovered == 0 and not allow_empty:
        raise EmptyDiscovery(f"no items discovered in {folder}; pass --allow-empty to permit")

    current = {
        row["locator"]: row
        for row in conn.execute(
            "SELECT i.id AS item_id, i.locator, v.id AS version_id, v.bytes_hash, v.tombstone"
            " FROM items i JOIN item_versions v ON v.item_id = i.id AND v.is_current = 1"
            " WHERE i.source_id = ?",
            (source_id,),
        )
    }

    added, changed, unchanged = [], [], []
    for locator, d in found.items():
        cur = current.get(locator)
        if cur is None or cur["tombstone"]:
            added.append(locator)
        elif cur["bytes_hash"] != d.bytes_hash:
            changed.append(locator)
        else:
            unchanged.append(locator)
    removed = [
        loc
        for loc, cur in current.items()
        if not cur["tombstone"] and loc not in found and loc not in quarantined
    ]

    # Blobs first, outside the transaction: content-addressed copies are harmless if a
    # later infrastructure fault fails the run, while source read errors quarantine only
    # that item.
    ready_added, ready_changed = [], []
    for bucket, ready in ((added, ready_added), (changed, ready_changed)):
        for locator in bucket:
            if cancel_event is not None and cancel_event.is_set():
                raise RunCancelled("interrupted by shutdown")
            d = found[locator]
            try:
                ws.blobs.put_file(d.path, d.bytes_hash)
            except SourceReadError as exc:
                quarantined[locator] = (exc.reason_code, str(exc), None)
            else:
                ready.append(locator)
    added, changed = ready_added, ready_changed
    result.quarantined = len(quarantined)

    now = utc_now()
    if result.discovered > 0 and result.quarantined == result.discovered:
        with transaction(conn):
            _record_quarantined(conn, source_id, result.run_id, current, quarantined, now)
        raise AllQuarantined(f"all {result.quarantined} discovered items were quarantined")

    with transaction(conn):
        _record_quarantined(conn, source_id, result.run_id, current, quarantined, now)
        for locator in added + changed:
            d = found[locator]
            cur = current.get(locator)
            if cur is None:
                existing = conn.execute(
                    "SELECT id FROM items WHERE source_id = ? AND locator = ?",
                    (source_id, locator),
                ).fetchone()
                item_id = (
                    existing["id"]
                    if existing is not None
                    else conn.execute(
                        "INSERT INTO items (source_id, locator) VALUES (?, ?)",
                        (source_id, locator),
                    ).lastrowid
                )
                predecessor = None
            else:
                item_id, predecessor = cur["item_id"], cur["version_id"]
                _retire(conn, predecessor)
            conn.execute(
                "INSERT INTO item_versions (item_id, run_id, predecessor_id, bytes_hash,"
                " size_bytes, role, tombstone, is_current, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)",
                (item_id, result.run_id, predecessor, d.bytes_hash, d.size_bytes, role, now),
            )
        for locator in removed:
            cur = current[locator]
            _retire(conn, cur["version_id"])
            conn.execute(
                "INSERT INTO item_versions (item_id, run_id, predecessor_id, role,"
                " tombstone, is_current, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)",
                (cur["item_id"], result.run_id, cur["version_id"], role, now),
            )

        result.added, result.changed = len(added), len(changed)
        result.unchanged, result.removed = len(unchanged), len(removed)
        result.status = "noop" if not (added or changed or removed or quarantined) else "succeeded"
        finish_run(
            conn, result.run_id, result.status,
            discovered=result.discovered, ignored=result.ignored,
            added=result.added, changed=result.changed,
            unchanged=result.unchanged, removed=result.removed,
            quarantined=result.quarantined,
        )  # fmt: skip


def _record_quarantined(
    conn: sqlite3.Connection,
    source_id: int,
    run_id: int,
    current: dict[str, sqlite3.Row],
    quarantined: dict[str, tuple[str, str, dict[str, Any] | None]],
    now: str,
) -> None:
    for locator, (reason_code, message, detail) in sorted(quarantined.items()):
        cur = current.get(locator)
        if cur is not None:
            item_id = cur["item_id"]
        else:
            existing = conn.execute(
                "SELECT id FROM items WHERE source_id = ? AND locator = ?",
                (source_id, locator),
            ).fetchone()
            item_id = (
                existing["id"]
                if existing is not None
                else conn.execute(
                    "INSERT INTO items (source_id, locator) VALUES (?, ?)",
                    (source_id, locator),
                ).lastrowid
            )
        entry: dict[str, Any] = {"code": reason_code, "message": message}
        if detail:
            entry["detail"] = detail
        diag = canonical([entry])
        conn.execute(
            "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key,"
            " item_version_id, input_fp, stage_fp, params, outcome, reason_code,"
            " diagnostics, created_at)"
            " VALUES (?, 'ingest', 'item', ?, NULL, '', '', '{}', 'quarantined', ?, ?, ?)",
            (run_id, f"item:{item_id}", reason_code, diag, now),
        )


def _retire(conn: sqlite3.Connection, version_id: int) -> None:
    conn.execute("UPDATE item_versions SET is_current = 0 WHERE id = ?", (version_id,))
