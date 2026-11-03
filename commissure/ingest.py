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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from commissure.limits import DEFAULT_LIMITS, LimitExceeded, Limits
from commissure.runner import AllQuarantined, RunCancelled, canonical
from commissure.runs import EmptyDiscovery, finish_run, start_run
from commissure.workspace import SourceReadError, Workspace, hash_file, transaction, utc_now

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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    """Return the id of source (kind, uri), creating it if new.

    A source keeps the role it was registered with; asking for a different one is an
    error rather than a silent re-labelling of existing items.
    """
    row = conn.execute(
        "SELECT id, role FROM sources WHERE kind = ? AND uri = ?", (kind, uri)
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
    source_id = register_source(conn, FOLDER, str(folder), role)
    run_id = start_run(conn, "ingest", trigger=trigger, config=config, source_id=source_id)
    result = IngestResult(run_id=run_id, status="running", source_id=source_id)
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
