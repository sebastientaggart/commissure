"""The run ledger: one row per invocation of a step, written before work starts so a
failed run is still recorded, and finished with its counts and status."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from commissure.workspace import transaction, utc_now

COUNTS = (
    "discovered",
    "ignored",
    "added",
    "changed",
    "unchanged",
    "removed",
    "attempted",
    "valid",
    "warning",
    "quarantined",
    "skipped",
)


class EmptyDiscovery(Exception):
    """Discovery found nothing and the run did not permit it."""


RUN_COLUMNS = ("stage_fp", "recipe_fp", "params", "resumes_run_id", "override")


def start_run(
    conn: sqlite3.Connection,
    step: str,
    *,
    trigger: str,
    config: dict[str, Any],
    source_id: int | None = None,
    **columns: Any,
) -> int:
    unknown = set(columns) - set(RUN_COLUMNS)
    if unknown:
        raise ValueError(f"unknown run columns: {sorted(unknown)}")
    names = ["step", "trigger", "status", "source_id", "started_at", "config", *columns]
    values = [
        step,
        trigger,
        "running",
        source_id,
        utc_now(),
        json.dumps(config, sort_keys=True),
        *columns.values(),
    ]
    with transaction(conn):
        return conn.execute(
            f"INSERT INTO runs ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            values,
        ).lastrowid


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str, error: str | None = None, **counts: int
) -> None:
    """Record the outcome. Runs inside the caller's transaction, if any."""
    unknown = set(counts) - set(COUNTS) - {"remaining"}
    if unknown:
        raise ValueError(f"unknown run counts: {sorted(unknown)}")
    columns = ["status = ?", "finished_at = ?", "error = ?"] + [f"{c} = ?" for c in counts]
    conn.execute(
        f"UPDATE runs SET {', '.join(columns)} WHERE id = ?",
        (status, utc_now(), error, *counts.values(), run_id),
    )
