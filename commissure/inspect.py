"""Read-only inspection of one or more workspaces.

Opens `state.db` with `mode=ro` and `query_only`. Does not take the workspace
lock, does not migrate, and does not create a missing workspace. SQLite may
create empty `-wal` and `-shm` sidecars in order to read a WAL database; it
does not modify `state.db`. Unmeasured run signals stay null; a step that does
not record a count is not reported as zero.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from commissure.workspace import BLOBS_DIR, DB_NAME, MIGRATIONS, BlobStore, WorkspaceError

# Columns a run may leave NULL. NULL means "this step does not measure it".
SIGNALS = (
    "discovered",
    "added",
    "changed",
    "unchanged",
    "removed",
    "ignored",
    "attempted",
    "valid",
    "warning",
    "quarantined",
    "skipped",
)

_RUN_FIELDS = (
    "id",
    "step",
    "status",
    "trigger",
    "source_id",
    "started_at",
    "finished_at",
    "error",
    *SIGNALS,
    "remaining",
    "resumes_run_id",
    "override",
    "params",
    "config",
)

_ISSUE_FIELDS = (
    "run_id",
    "stage",
    "subject_kind",
    "subject_key",
    "source_id",
    "locator",
    "item_version_id",
    "is_current",
    "outcome",
    "reason_code",
    "diagnostics",
)


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open `db_path` for queries only. The connection cannot create or migrate it."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise WorkspaceError(f"cannot read {db_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def inspect_workspaces(
    roots: list[Path],
    *,
    run: str | None = None,
    stage: str | None = None,
    subject: str | None = None,
    call: int | None = None,
    only_errors: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    return {
        "workspaces": [
            inspect_workspace(
                root,
                run=run,
                stage=stage,
                subject=subject,
                call=call,
                only_errors=only_errors,
                limit=limit,
            )
            for root in roots
        ]
    }


def inspect_workspace(
    root: Path,
    *,
    run: str | None = None,
    stage: str | None = None,
    subject: str | None = None,
    call: int | None = None,
    only_errors: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise WorkspaceError(f"workspace path {root} is not a directory")
    db_path = root / DB_NAME
    if not db_path.is_file():
        raise WorkspaceError(f"{root} has no {DB_NAME}")
    conn = open_readonly(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != len(MIGRATIONS):
            raise WorkspaceError(
                f"{root}: schema version {version}, this code expects {len(MIGRATIONS)}; "
                "inspect does not migrate"
            )
        all_runs = _runs(conn, "runs")
        target_run_id = _resolve_run_selector(all_runs, run, stage=stage, only_errors=only_errors)
        call_detail = _call_detail(conn, call) if call is not None else None
        if run is None and call is not None and call_detail is not None:
            target_run_id = call_detail["run_id"]
        has_run_scope = run is not None or (call is not None and call_detail is not None)
        subject_keys = _resolve_subject_keys(conn, subject) if subject is not None else None
        subject_runs = _subject_run_ids(conn, subject_keys) if subject_keys is not None else None

        runs = [
            r
            for r in all_runs
            if (not has_run_scope or r["id"] == target_run_id)
            and _stage_matches(r["step"], stage)
            and (not only_errors or r["status"] == "failed")
            and (subject_runs is None or r["id"] in subject_runs)
        ]
        if limit is not None:
            runs = runs[-limit:]

        outcomes = [
            r
            for r in _outcomes(conn)
            if _stage_matches(r["stage"], stage)
            and (not only_errors or r["outcome"] == "quarantined")
        ]

        issues = [
            r
            for r in _issues(conn)
            if (not has_run_scope or r["run_id"] == target_run_id)
            and _stage_matches(r["stage"], stage)
            and (subject_keys is None or r["subject_key"] in subject_keys)
            and (not only_errors or r["outcome"] == "quarantined")
        ]
        if limit is not None:
            issues = issues[-limit:]

        llm_by_run = [
            r
            for r in _llm_calls_by_run(conn)
            if (not has_run_scope or r["run_id"] == target_run_id)
            and _stage_matches(r["stage"], stage)
        ]

        llm_calls = [
            r
            for r in _llm_calls(conn)
            if (call is None or r["id"] == call)
            and (not has_run_scope or r["run_id"] == target_run_id)
            and _stage_matches(r["stage"], stage)
            and (subject_keys is None or r["subject_key"] in subject_keys)
            and (not only_errors or r["status"] != "ok")
        ]
        if limit is not None:
            llm_calls = llm_calls[-limit:]

        payload: dict[str, Any] = {
            "workspace": str(root),
            "schema_version": version,
            "counts": _counts(conn),
            "runs": runs,
            "latest_runs": _runs(conn, "latest_runs"),
            "outcomes": outcomes,
            "drift": _drift(conn),
            "issues": issues,
            "llm_calls_by_run": llm_by_run,
            "llm_calls_by_model": _llm_calls_by_model(conn),
            "llm_calls": llm_calls,
        }
        if subject_keys is not None:
            subj_outcomes = _subject_outcomes(
                conn,
                subject_keys,
                run_id=target_run_id if has_run_scope else None,
                stage=stage,
                only_errors=only_errors,
            )
            if limit is not None:
                subj_outcomes = subj_outcomes[-limit:]
            payload["subject_outcomes"] = subj_outcomes
            payload["subject_detail"] = _subject_detail(conn, root, subject_keys)
        if call is not None:
            payload["call_detail"] = call_detail
        return payload
    except sqlite3.Error as exc:
        raise WorkspaceError(f"{root}: {exc}") from exc
    finally:
        conn.close()


def render(report: dict[str, Any], console: Console) -> None:
    """Tables for a person. `--json` is the full report; unmeasured signals render as `-`."""
    for index, ws in enumerate(report["workspaces"]):
        if index:
            console.print()
        console.print(f"[bold]{escape(ws['workspace'])}[/bold]  schema {ws['schema_version']}")
        counts = ws["counts"]
        console.print(
            f"{counts['sources']} sources, {counts['current_items']} current items, "
            f"{counts['item_versions']} versions, {counts['tombstones']} tombstones, "
            f"{counts['entities']} entities, {counts['edges']} edges"
        )
        if counts["by_role"]:
            roles = Table("role", "current items", title="Items by role", title_justify="left")
            for role, n in counts["by_role"].items():
                roles.add_row(escape(role), str(n))
            console.print(roles)
        latest = ", ".join(f"{row['step']}#{row['id']}" for row in ws["latest_runs"]) or "-"
        console.print(f"latest runs: {escape(latest)}")
        _table(
            console,
            "Runs",
            ["id", "step", "status", "source", *SIGNALS],
            [
                [
                    row["id"],
                    row["step"],
                    row["status"],
                    row["source_id"],
                    *[row[name] for name in SIGNALS],
                ]
                for row in ws["runs"]
            ],
        )
        _table(
            console,
            "Current outcomes",
            ["stage", "outcome", "n", "version_stale", "code_stale", "params_stale", "input_stale"],
            [
                [
                    row["stage"],
                    row["outcome"],
                    row["n"],
                    row["version_stale"],
                    row["code_stale"],
                    row["params_stale"],
                    row["input_stale"],
                ]
                for row in ws["outcomes"]
            ],
        )
        _table(
            console,
            "Drift",
            ["change", "change_kind", "n"],
            [[row["change"], row["change_kind"], row["n"]] for row in ws["drift"]],
        )
        _table(
            console,
            "Issues",
            ["locator", "stage", "outcome", "reason"],
            [
                [
                    row["locator"] or row["subject_key"],
                    row["stage"],
                    row["outcome"],
                    row["reason_code"],
                ]
                for row in ws["issues"]
            ],
        )
        _table(
            console,
            "LLM calls by run",
            [
                "run_id",
                "stage",
                "calls",
                "ok",
                "invalid",
                "err",
                "refused",
                "cached",
                "in_tok",
                "out_tok",
                "cost_usd",
                "latency_ms",
            ],
            [
                [
                    row["run_id"],
                    row["stage"],
                    row["calls"],
                    row["ok_calls"],
                    row["invalid_calls"],
                    row["transport_error_calls"],
                    row["refused_calls"],
                    row["cached_calls"],
                    row["input_tokens"],
                    row["output_tokens"],
                    row["cost_usd"],
                    row["latency_ms"],
                ]
                for row in ws["llm_calls_by_run"]
            ],
        )
        _table(
            console,
            "LLM calls by model",
            [
                "provider",
                "model",
                "calls",
                "ok",
                "invalid",
                "err",
                "refused",
                "cached",
                "in_tok",
                "out_tok",
                "cost_usd",
                "latency_ms",
            ],
            [
                [
                    row["provider"],
                    row["model"],
                    row["calls"],
                    row["ok_calls"],
                    row["invalid_calls"],
                    row["transport_error_calls"],
                    row["refused_calls"],
                    row["cached_calls"],
                    row["input_tokens"],
                    row["output_tokens"],
                    row["cost_usd"],
                    row["latency_ms"],
                ]
                for row in ws["llm_calls_by_model"]
            ],
        )
        _table(
            console,
            "LLM calls",
            [
                "id",
                "run_id",
                "stage",
                "subject",
                "provider",
                "model",
                "status",
                "cached",
                "repairs",
                "in_tok",
                "out_tok",
                "cost_usd",
                "latency_ms",
                "error",
            ],
            [
                [
                    row["id"],
                    row["run_id"],
                    row["stage"],
                    row["subject_key"],
                    row["provider"],
                    row["model"],
                    row["status"],
                    row["cached"],
                    row["repairs_count"],
                    row["input_tokens"],
                    row["output_tokens"],
                    row["cost_usd"],
                    row["latency_ms"],
                    row["error"],
                ]
                for row in ws["llm_calls"]
            ],
        )
        for row in ws["runs"]:
            if row.get("error"):
                step_str = escape(str(row["step"]))
                err_str = escape(str(row["error"]))
                console.print(f"run #{row['id']} ({step_str}) error: {err_str}")
        for row in ws["llm_calls"]:
            if row.get("error"):
                console.print(
                    f"llm_call #{row['id']} ({escape(str(row['stage']))} "
                    f"{escape(str(row['subject_key']))} {escape(str(row['status']))}): "
                    f"{escape(str(row['error']))}"
                )
        if "subject_outcomes" in ws:
            _table(
                console,
                "Subject outcomes",
                ["id", "run_id", "stage", "subject", "locator", "outcome", "reason"],
                [
                    [
                        row["id"],
                        row["run_id"],
                        row["stage"],
                        row["subject_key"],
                        row["locator"],
                        row["outcome"],
                        row["reason_code"],
                    ]
                    for row in ws["subject_outcomes"]
                ],
            )
        if "subject_detail" in ws:
            _render_subject(console, ws["subject_detail"])
        if "call_detail" in ws and ws["call_detail"] is not None:
            detail = ws["call_detail"]
            console.print(
                f"call #{detail['id']} cache_key={escape(str(detail['cache_key']))} "
                f"cached_entry_present={detail['cached_entry_present']}"
            )


_TEXT_PREVIEW_CHARS = 300  # terminal preview only; --json carries the full text


def _render_subject(console: Console, detail: dict[str, Any]) -> None:
    # soft_wrap: a path or link broken across lines can no longer be clicked or copied.
    def line(text: str) -> None:
        console.print(text, soft_wrap=True, highlight=False)

    line(f"[bold]Subject[/bold] {escape(', '.join(detail['keys']))}")
    for item in detail["items"]:
        state = "removed" if item["tombstone"] else f"{item['size_bytes']:,} bytes"
        line(
            f"[bold]{item['key']}[/bold]  {escape(item['locator'])}  "
            f"({escape(item['role'])}, {state}, version {item['item_version_id']})"
        )
        if item["original_path"] is not None:
            status = {
                "present": "",
                "missing": "  [missing; the blob holds the ingested bytes]",
                "changed": "  [changed since ingest; the blob holds the ingested bytes]",
            }[item["original_status"]]
            line(f"  original  {escape(item['original_path'])}{escape(status)}")
        if item["blob_path"] is not None:
            absent = "" if item["blob_present"] else "  [missing]"
            line(f"  blob      {escape(item['blob_path'])}{escape(absent)}")
        for p in item["parse"]:
            reason = f" ({p['reason_code']})" if p["reason_code"] else ""
            line(f"  {escape(p['stage'])}: {escape(p['outcome'] + reason)}")
        for page in item["pages"]:
            findings = "; ".join(_finding_text(f) for f in page["findings"])
            line(f"  page {page['page']}: {escape(findings)}")
            if page["url"]:
                line(f"    {escape(page['url'])}")
        for text in item["texts"]:
            line(
                f"  text {escape(text['parser'])} {escape(text['parser_version'])}:"
                f" {text['chars']:,} chars"
            )
            if text["chars"]:
                preview = " ".join(text["text"][:_TEXT_PREVIEW_CHARS].split())
                shown = min(text["chars"], _TEXT_PREVIEW_CHARS)
                line(f"    {escape(preview)}")
                if text["chars"] > shown:
                    line(f"    (first {shown} of {text['chars']:,} chars; full text in --json)")
    for rec in detail["records"]:
        stale = [n.removesuffix("_stale") for n in _STALE_FLAGS if rec[n]]
        flags = f"  stale: {', '.join(stale)}" if stale else ""
        older = "  (from an earlier run; the latest outcome has no record)"
        older = older if rec["fallback"] else ""
        line(
            f"[bold]record {escape(rec['stage'])}[/bold] {escape(rec['subject_key'])}  "
            f"{escape(rec['outcome'])}  run {rec['run_id']}{escape(flags + older)}"
        )
        line(escape(json.dumps(rec["payload"], indent=2, ensure_ascii=False)))
    # Parse page evidence is already listed per item above (with its reasons); this lists
    # what records point at.
    for ev in (e for e in detail["evidence"] if e["origin"] == "record"):
        where = escape(ev["locator"]) + (f" p. {ev['page']}" if ev["page"] else "")
        trust = f"  [ocr, confidence {_short(ev['confidence'])}]" if ev["via_ocr"] else ""
        copied = "  (copied from an upstream record)" if ev["copied"] else ""
        line(
            f"[bold]evidence {escape(ev['stage'])}[/bold] {escape(ev['pointer'])}  "
            f"{escape(ev['kind'])}  {where}{escape(trust + copied)}"
        )
        if ev["supports"] is not None:
            supports = json.dumps(ev["supports"], ensure_ascii=False)
            line(f"  supports  {escape(supports)}")
        if ev["span_text"] is not None:
            before = " ".join((ev["context_before"] or "").split())
            after = " ".join((ev["context_after"] or "").split())
            span = " ".join(ev["span_text"].split())
            line(f"  in text   ...{escape(before)} [bold]{escape(span)}[/bold] {escape(after)}...")
        else:
            line("  in text   (the item as a whole: no exact span; search the original for it)")
        if ev["url"]:
            line(f"  {escape(ev['url'])}")
        elif ev["original_status"] != "present":
            line(f"  original {escape(ev['original_path'])} is {ev['original_status']}")


_STALE_FLAGS = ("version_stale", "code_stale", "params_stale", "input_stale")


def _finding_text(finding: dict[str, Any]) -> str:
    extras = [
        f"{name} {', '.join(map(str, value)) if isinstance(value, list) else _short(value)}"
        for name, value in finding.items()
        if name not in ("stage", "code")
    ]
    return f"{finding['stage']} {finding['code']}" + (f" ({'; '.join(extras)})" if extras else "")


def _short(value: Any) -> str:
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def _table(console: Console, title: str, headers: list[str], rows: list[list[Any]]) -> None:
    table = Table(*headers, title=f"{title} ({len(rows)})", title_justify="left")
    for row in rows:
        table.add_row(*(_cell(value) for value in row))
    console.print(table)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return escape(str(value))


def _counts(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM sources) AS sources,
            (SELECT count(*) FROM current_items) AS current_items,
            (SELECT count(*) FROM item_versions) AS item_versions,
            (SELECT count(*) FROM item_versions WHERE tombstone = 1) AS tombstones,
            (SELECT count(*) FROM entities) AS entities,
            (SELECT count(*) FROM edges) AS edges
        """
    ).fetchone()
    by_role = {
        r["role"]: r["n"]
        for r in conn.execute(
            "SELECT role, count(*) AS n FROM current_items GROUP BY role ORDER BY role"
        )
    }
    return {**dict(row), "by_role": by_role}


def _runs(conn: sqlite3.Connection, relation: str) -> list[dict[str, Any]]:
    if relation not in ("runs", "latest_runs"):
        raise ValueError(relation)
    columns = ", ".join(_RUN_FIELDS)
    rows = []
    for row in conn.execute(f"SELECT {columns} FROM {relation} ORDER BY id"):
        item = dict(row)
        item["params"] = _json_value(item["params"])
        item["config"] = _json_value(item["config"])
        rows.append(item)
    return rows


def _outcomes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT stage, outcome, count(*) AS n,
               sum(version_stale) AS version_stale,
               sum(code_stale) AS code_stale,
               sum(params_stale) AS params_stale,
               sum(input_stale) AS input_stale
        FROM current_outcomes
        GROUP BY stage, outcome
        ORDER BY stage, outcome
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _drift(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT change, change_kind, count(*) AS n
        FROM drift
        GROUP BY change, change_kind
        ORDER BY change, change_kind
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = ", ".join(_ISSUE_FIELDS)
    rows = []
    for row in conn.execute(
        f"SELECT {columns} FROM outcome_issues ORDER BY run_id, stage, subject_key"
    ):
        item = dict(row)
        item["diagnostics"] = _json_value(item["diagnostics"])
        rows.append(item)
    return rows


def _llm_calls_by_run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM llm_calls_by_run ORDER BY run_id, stage").fetchall()
    return [dict(row) for row in rows]


def _llm_calls_by_model(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM llm_calls_by_model ORDER BY provider, model").fetchall()
    return [dict(row) for row in rows]


def _llm_calls(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, run_id, stage, subject_key, provider, model, status, input_tokens,"
        " output_tokens, cost_usd, latency_ms, repairs_count, cached, error, created_at"
        " FROM llm_calls ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def _stage_matches(row_stage: str | None, stage_filter: str | None) -> bool:
    if stage_filter is None:
        return True
    if row_stage is None:
        return False
    sf = stage_filter.strip()
    if not sf:
        return True
    return (
        row_stage == sf
        or row_stage.endswith(f".{sf}")
        or (sf == "parse" and row_stage.startswith("parse."))
    )


def _resolve_run_selector(
    runs: list[dict[str, Any]],
    selector: str | None,
    *,
    stage: str | None = None,
    only_errors: bool = False,
) -> int | None:
    if selector is None:
        return None
    sel = selector.strip()
    scoped = [r for r in runs if _stage_matches(r["step"], stage)]
    if sel == "latest":
        candidates = [r for r in scoped if not only_errors or r["status"] == "failed"]
        return candidates[-1]["id"] if candidates else None
    if sel == "failed":
        failed = [r for r in scoped if r["status"] == "failed"]
        return failed[-1]["id"] if failed else None
    if sel.isdigit() and int(sel) >= 1:
        return int(sel)
    raise ValueError(f"--run must be a positive run id, 'latest', or 'failed'; got {selector!r}")


_AMBIGUOUS_SHOWN = 20  # matches listed in the error; the rest are counted, never dropped silently


def _resolve_subject_keys(conn: sqlite3.Connection, subject: str) -> set[str]:
    """Subject keys for `subject`; a usage error when nothing or more than one item matches.

    A bare name may be a group key, an item id, or a locator. An exact locator wins; a
    file-name (suffix) match counts only when exactly one item has it, because a QA check on
    the wrong file is worse than no answer. The suffix is compared with `substr`, not `LIKE`,
    so `_` and `%` in file names are literal.
    """
    subj = subject.strip()
    if not subj:
        raise ValueError("--subject is empty")
    keys = {subj}
    if ":" not in subj:
        keys.add(f"group:{subj}")
        if subj.isdigit():
            keys.add(f"item:{subj}")
        sql = (
            "SELECT i.id, i.locator, s.uri FROM items i JOIN sources s ON s.id = i.source_id"
            " WHERE {} ORDER BY i.id"
        )
        matches = conn.execute(sql.format("i.locator = ?"), (subj,)).fetchall()
        if not matches:
            matches = conn.execute(
                sql.format("substr(i.locator, -?) = ?"), (len(subj) + 1, f"/{subj}")
            ).fetchall()
        if len(matches) > 1:
            shown = "\n".join(
                f"  item:{r['id']}  {r['uri']}/{r['locator']}" for r in matches[:_AMBIGUOUS_SHOWN]
            )
            more = len(matches) - _AMBIGUOUS_SHOWN
            raise ValueError(
                f"--subject {subject!r} matches {len(matches)} items; pass the full locator"
                f" or one item key:\n{shown}" + (f"\n  ... and {more} more" if more > 0 else "")
            )
        keys.update(f"item:{r['id']}" for r in matches)
    known = {k for k in keys if _subject_known(conn, k)}
    if not known:
        raise ValueError(f"--subject {subject!r} matches no item, outcome, or model call")
    return known


def _subject_known(conn: sqlite3.Connection, key: str) -> bool:
    is_item = key.startswith("item:") and key[5:].isdigit()
    if is_item and conn.execute("SELECT 1 FROM items WHERE id = ?", (int(key[5:]),)).fetchone():
        return True
    for table in ("outcomes", "llm_calls"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE subject_key = ? LIMIT 1", (key,)).fetchone():
            return True
    return False


def _item_ids(keys: set[str]) -> list[int]:
    return sorted(int(k[5:]) for k in keys if k.startswith("item:") and k[5:].isdigit())


def _subject_run_ids(conn: sqlite3.Connection, keys: set[str]) -> set[int]:
    """Runs that touched the subject: its outcomes, its model calls, its items' ingests."""
    ordered = sorted(keys)
    marks = ",".join("?" * len(ordered))
    item_ids = _item_ids(keys)
    item_marks = ",".join("?" * len(item_ids)) or "NULL"
    rows = conn.execute(
        f"SELECT run_id FROM outcomes WHERE subject_key IN ({marks})"
        f" UNION SELECT run_id FROM llm_calls WHERE subject_key IN ({marks})"
        f" UNION SELECT run_id FROM item_versions WHERE item_id IN ({item_marks})",
        [*ordered, *ordered, *item_ids],
    )
    return {r[0] for r in rows}


def _subject_detail(conn: sqlite3.Connection, root: Path, keys: set[str]) -> dict[str, Any]:
    """Everything needed to check a subject by hand: originals, pages, texts, records, evidence."""
    blobs = BlobStore(root / BLOBS_DIR)
    return {
        "keys": sorted(keys),
        "items": [_item_detail(conn, blobs, v) for v in _subject_item_versions(conn, keys)],
        "records": _subject_records(conn, keys),
        "evidence": _subject_evidence(conn, keys),
    }


_CONTEXT_CHARS = 80  # text shown on each side of a span


def _subject_evidence(
    conn: sqlite3.Connection, keys: set[str], *, under: str | None = None
) -> list[dict[str, Any]]:
    """The subject's current evidence (`evidence_review`), ready to check against the original.

    Each record pointer comes with what it supports (the payload object holding it, minus
    the evidence itself), the span with text on either side, and a page link while the
    original still matches the ingested bytes. Parse page evidence is listed too. `under`
    keeps only record pointers at or below one payload pointer (one judged field).

    Context is sliced in SQL and each payload decoded once: one comment can carry over a
    thousand pointers into a few large texts, and per-row Python work took seconds.
    """
    ordered = sorted(keys)
    marks = ",".join("?" * len(ordered))
    where, params = f"er.subject_key IN ({marks})", list(ordered)
    if under is not None:
        where += " AND er.origin = 'record' AND (er.pointer = ? OR substr(er.pointer, 1, ?) = ?)"
        params += [under, len(under) + 1, under + "/"]
    rows = conn.execute(
        f"""
        SELECT er.*, v.size_bytes, o.record_id,
               CASE WHEN er.span_start IS NOT NULL THEN substr(t.text,
                    max(er.span_start - {_CONTEXT_CHARS}, 0) + 1,
                    er.span_start - max(er.span_start - {_CONTEXT_CHARS}, 0)) END
                   AS context_before,
               CASE WHEN er.span_start IS NOT NULL
                    THEN substr(t.text, er.span_end + 1, {_CONTEXT_CHARS}) END AS context_after
        FROM evidence_review er
        JOIN item_versions v ON v.id = er.item_version_id
        JOIN outcomes o ON o.id = er.outcome_id
        LEFT JOIN extracted_text t ON t.id = er.extracted_text_id AND er.span_start IS NOT NULL
        WHERE {where}
        ORDER BY er.stage, er.subject_key, er.origin DESC, er.evidence_id, er.page, er.kind
        """,
        params,
    ).fetchall()
    payloads: dict[int, Any] = {}
    for record_id in {r["record_id"] for r in rows if r["origin"] == "record"}:
        payloads[record_id] = _json_value(
            conn.execute("SELECT payload FROM records WHERE id = ?", (record_id,)).fetchone()[0]
        )
    statuses: dict[str, tuple[str | None, str | None]] = {}
    out = []
    for r in rows:
        original = Path(r["source_uri"]) / r["locator"]
        if str(original) not in statuses:
            status = _original_status(original, r["size_bytes"])
            statuses[str(original)] = (status, original.as_uri() if status == "present" else None)
        status, link = statuses[str(original)]
        entry = {
            k: r[k]
            for k in (
                "origin", "stage", "subject_key", "outcome", "pointer", "kind", "copied",
                "item_version_id", "locator", "page", "via_ocr", "confidence",
                "span_start", "span_end", "span_text", "context_before", "context_after",
            )
        }  # fmt: skip
        entry["via_ocr"] = bool(r["via_ocr"])
        entry["original_path"] = str(original)
        entry["original_status"] = status
        entry["url"] = f"{link}#page={r['page']}" if link and r["page"] else link
        entry["supports"] = (
            _supported(payloads[r["record_id"]], r["pointer"]) if r["origin"] == "record" else None
        )
        out.append(entry)
    return out


def _supported(payload: Any, pointer: str | None) -> Any:
    """The payload object an evidence pointer sits in, without its evidence field.

    `/quotes/0/evidence/1` is supported by `/quotes/0`; `/finding/evidence` by `/finding`.
    """
    if payload is None or pointer is None:
        return None
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]
    path, value = [], payload
    for token in tokens:
        path.append(value)
        value = value[int(token)] if isinstance(value, list) else value[token]
    owner_index = len(tokens) - (2 if tokens[-1].isdigit() and len(tokens) > 1 else 1)
    owner = path[owner_index]
    field = tokens[owner_index]
    return {k: v for k, v in owner.items() if k != field} if isinstance(owner, dict) else owner


def _subject_item_versions(conn: sqlite3.Connection, keys: set[str]) -> list[int]:
    """Item versions behind the subject, ordered by locator.

    An item subject is its current version. A group subject is every item version its
    current outcomes consumed, followed back through `outcome_inputs`: that is what the
    finding rests on. Run subjects are not followed, since they consume the whole scope.
    """
    item_ids = _item_ids(keys)
    groups = sorted(k for k in keys if k.startswith("group:"))
    item_marks = ",".join("?" * len(item_ids)) or "NULL"
    group_marks = ",".join("?" * len(groups)) or "NULL"
    rows = conn.execute(
        f"""
        WITH RECURSIVE reach(id) AS (
            SELECT outcome_id FROM current_outcomes
            WHERE subject_kind = 'group' AND subject_key IN ({group_marks})
            UNION
            SELECT oi.upstream_outcome_id FROM outcome_inputs oi
            JOIN reach ON oi.outcome_id = reach.id
        ),
        versions(id) AS (
            SELECT o.item_version_id FROM reach JOIN outcomes o ON o.id = reach.id
            WHERE o.item_version_id IS NOT NULL
            UNION
            SELECT id FROM item_versions WHERE is_current = 1 AND item_id IN ({item_marks})
        )
        SELECT v.id FROM versions JOIN item_versions v ON v.id = versions.id
        JOIN items i ON i.id = v.item_id
        ORDER BY i.locator, i.source_id, v.id
        """,
        [*groups, *item_ids],
    )
    return [r[0] for r in rows]


def _item_detail(
    conn: sqlite3.Connection, blobs: BlobStore, version_id: int, *, texts: bool = True
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT v.id AS item_version_id, v.item_id, v.role, v.bytes_hash, v.size_bytes,
               v.tombstone, v.is_current, v.run_id AS ingest_run_id, i.locator, i.source_id,
               s.kind AS source_kind, s.uri AS source_uri
        FROM item_versions v JOIN items i ON i.id = v.item_id JOIN sources s ON s.id = i.source_id
        WHERE v.id = ?
        """,
        (version_id,),
    ).fetchone()
    detail = dict(row)
    detail["key"] = f"item:{row['item_id']}"
    original = Path(row["source_uri"]) / row["locator"] if row["source_kind"] == "folder" else None
    detail["original_path"] = str(original) if original is not None else None
    detail["original_status"] = _original_status(original, row["size_bytes"])
    blob = blobs.path_for(row["bytes_hash"]) if row["bytes_hash"] else None
    detail["blob_path"] = str(blob) if blob is not None else None
    detail["blob_present"] = blob is not None and blob.is_file()

    parse = conn.execute(
        """
        SELECT o.stage, o.outcome, o.reason_code, o.diagnostics FROM outcomes o
        WHERE o.item_version_id = ? AND o.stage LIKE 'parse.%'
          AND o.id = (SELECT max(o2.id) FROM outcomes o2
                      WHERE o2.item_version_id = o.item_version_id AND o2.stage = o.stage)
        ORDER BY o.stage
        """,
        (version_id,),
    ).fetchall()
    detail["parse"] = [
        {"stage": p["stage"], "outcome": p["outcome"], "reason_code": p["reason_code"]}
        for p in parse
    ]
    # A link opens the original only while it is the file that was parsed; a missing or
    # changed original gets no link, because its pages may not be the pages judged.
    present = original is not None and detail["original_status"] == "present"
    link = original.as_uri() if present else None
    detail["pages"] = _page_findings(parse, link)

    if not texts:
        return detail
    detail["texts"] = [
        {**dict(t), "chars": len(t["text"])}
        for t in conn.execute(
            """
            SELECT et.id AS extracted_text_id, et.parser, et.parser_version, et.text_hash, et.text
            FROM extracted_text et
            WHERE et.item_version_id = ?
              AND et.id = (SELECT max(e2.id) FROM extracted_text e2
                           WHERE e2.item_version_id = et.item_version_id AND e2.parser = et.parser)
            ORDER BY et.parser
            """,
            (version_id,),
        )
    ]
    return detail


def _original_status(original: Path | None, size_bytes: int | None) -> str | None:
    """`present`, `missing`, or `changed` (size differs from the ingested bytes)."""
    if original is None:
        return None
    if not original.is_file():
        return "missing"
    return "present" if original.stat().st_size == size_bytes else "changed"


def _page_findings(parse_outcomes: list[sqlite3.Row], link: str | None) -> list[dict[str, Any]]:
    """Every page a parse diagnostic names, with each finding and its per-page detail.

    Generic over diagnostic codes: any diagnostic whose `detail.pages` lists page numbers
    counts, and any `detail` mapping keyed by page (`reasons`, `confidence`) is attached.
    """
    by_page: dict[int, list[dict[str, Any]]] = {}
    for p in parse_outcomes:
        for diag in _json_value(p["diagnostics"]) or []:
            detail = diag.get("detail") or {}
            pages = detail.get("pages")
            if not isinstance(pages, list):
                continue
            for page in pages:
                if not isinstance(page, int):
                    continue
                finding = {"stage": p["stage"], "code": diag.get("code")}
                for name, value in detail.items():
                    if name != "pages" and isinstance(value, dict) and str(page) in value:
                        finding[name] = value[str(page)]
                by_page.setdefault(page, []).append(finding)
    return [
        {"page": page, "url": f"{link}#page={page}" if link else None, "findings": findings}
        for page, findings in sorted(by_page.items())
    ]


def _subject_records(conn: sqlite3.Connection, keys: set[str]) -> list[dict[str, Any]]:
    ordered = sorted(keys)
    marks = ",".join("?" * len(ordered))
    rows = conn.execute(
        f"""
        SELECT stage, subject_key, run_id, outcome, reason_code, record_id, payload,
               fallback, latest_outcome, version_stale, code_stale, params_stale, input_stale
        FROM current_records WHERE subject_key IN ({marks}) ORDER BY stage, subject_key
        """,
        ordered,
    ).fetchall()
    return [{**dict(r), "payload": _json_value(r["payload"])} for r in rows]


def _subject_outcomes(
    conn: sqlite3.Connection,
    subject_keys: set[str],
    *,
    run_id: int | None = None,
    stage: str | None = None,
    only_errors: bool = False,
) -> list[dict[str, Any]]:
    if not subject_keys:
        return []
    ordered_keys = sorted(subject_keys)
    marks = ",".join("?" * len(ordered_keys))
    rows = conn.execute(
        f"""
        SELECT o.id, o.run_id, o.stage, o.subject_kind, o.subject_key,
               i.locator, o.item_version_id, o.outcome, o.reason_code,
               o.diagnostics, o.record_id, o.extracted_text_id, o.created_at
        FROM outcomes o
        LEFT JOIN item_versions v ON v.id = o.item_version_id
        LEFT JOIN items i ON i.id = coalesce(
            v.item_id,
            CASE WHEN o.subject_kind = 'item' AND substr(o.subject_key, 1, 5) = 'item:'
                      AND substr(o.subject_key, 6) GLOB '[0-9]*'
                 THEN CAST(substr(o.subject_key, 6) AS INTEGER) END
        )
        WHERE o.subject_key IN ({marks})
        ORDER BY o.id
        """,
        ordered_keys,
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if run_id is not None and row["run_id"] != run_id:
            continue
        if not _stage_matches(row["stage"], stage):
            continue
        if only_errors and row["outcome"] != "quarantined":
            continue
        item = dict(row)
        item["diagnostics"] = _json_value(item["diagnostics"])
        result.append(item)
    return result


def _call_detail(conn: sqlite3.Connection, call_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT l.id, l.run_id, l.stage, l.subject_key, l.provider, l.model,
               l.cache_key, l.request_hash, l.prompt_hash, l.status,
               l.input_tokens, l.output_tokens, l.cost_usd, l.latency_ms,
               l.repairs_count, l.cached, l.error, l.created_at,
               c.request_json, c.raw_response
        FROM llm_calls l
        LEFT JOIN llm_cache c ON c.cache_key = l.cache_key
        WHERE l.id = ?
        """,
        (call_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "stage": row["stage"],
        "subject_key": row["subject_key"],
        "provider": row["provider"],
        "model": row["model"],
        "cache_key": row["cache_key"],
        "request_hash": row["request_hash"],
        "prompt_hash": row["prompt_hash"],
        "status": row["status"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cost_usd": row["cost_usd"],
        "latency_ms": row["latency_ms"],
        "repairs_count": row["repairs_count"],
        "cached": row["cached"],
        "error": row["error"],
        "created_at": row["created_at"],
        "cached_entry_present": row["request_json"] is not None,
        "request": _json_value(row["request_json"]),
        "raw_response": row["raw_response"],
    }


def _json_value(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)
