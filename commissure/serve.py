"""Single-process local HTTP mount for recipe interfaces (`AGENTS.md` Section 1).

A recipe opts in by defining `INTERFACE = Interface(...)` in `recipe.py` (imported from
`commissure.api`). Core mounts that interface on `127.0.0.1` using stdlib `http.server`,
serves vendored assets under `/static/` and read-only stage outputs under `/outputs/`,
computes the ordered pipeline status strip directly from `Recipe.stages` and `state.db`,
and executes recipe actions and pipeline steps in-process under the workspace lock.

If a recipe defines no `INTERFACE`, `commissure serve` exits with a usage error: there are
no developer web routes without a recipe interface (`AGENTS.md`).
"""

from __future__ import annotations

import fcntl
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from pydantic import BaseModel, ValidationError

from commissure.deliver.html import (
    _INLINE_BRAND,
    STATIC_DIR,
    _fragment,
    external_links_open_in_new_tab,
    page_external_resource_count,
)
from commissure.deliver.html import (
    DEFAULT_PAGE_FOOTER as DEFAULT_INTERFACE_FOOTER,
)
from commissure.ingest import check_folder_source, effective_ignore, ingest_folder
from commissure.inspect import open_readonly
from commissure.limits import Limits, is_limit_quarantine_stale, resolve_limits
from commissure.llm import validate_stage_llm
from commissure.parse import DEFAULT_FOR, PARSERS, ParserUnavailable, extension
from commissure.parse.step import parse_plan
from commissure.progress import estimate_remaining_seconds, format_remaining
from commissure.recipes import (
    Interface,
    Recipe,
    RecipeError,
    Stage,
    load_recipe,
    parsers_for,
    recipe_fingerprint,
    recipe_plan,
    register_stages,
    text_parsers,
)
from commissure.review_page import ReviewPages
from commissure.runner import RunCancelled, reap_abandoned, run_plan
from commissure.workspace import (
    DB_NAME,
    LOCK_NAME,
    MIGRATIONS,
    OUTPUTS_DIR,
    WorkspaceError,
    WorkspaceLocked,
    check_root,
    lock,
    open_workspace,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "deliver" / "templates"
_MAX_FORM_BYTES = 1 << 20
_DEFAULT_WORKERS = min(4, os.cpu_count() or 1)

_MIME_OVERRIDES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
}


class ActionError(Exception):
    """Expected user, configuration, lock, or step failure surfaced as an interface alert."""


@dataclass(frozen=True)
class SourceInfo:
    id: int
    kind: str
    uri: str
    role: str
    created_at: str
    item_count: int
    last_discovered_count: int | None = None
    latest_item_run_id: int = 0
    latest_parse_run_id: int = 0
    unparsed_item_count: int = 0


@dataclass(frozen=True)
class StepStatus:
    key: str
    label: str
    kind: str
    order: int
    status: str
    summary: str
    needs: tuple[str, ...] = ()
    roles: tuple[str, ...] | None = None
    uses_llm: bool = False
    valid: int | None = None
    warning: int | None = None
    quarantined: int | None = None
    skipped: int | None = None
    pending: int = 0
    stale: int = 0
    runnable: bool = True
    recommended_next: bool = False
    error: str | None = None


@dataclass(frozen=True)
class RecordInfo:
    stage: str
    subject_kind: str
    subject_key: str
    run_id: int
    outcome: str
    reason_code: str | None
    payload: BaseModel
    stale: bool
    stale_reasons: tuple[str, ...]
    _outputs_root: Path = field(repr=False)

    def output_url(self, relative_path: str) -> str | None:
        """Return `/outputs/<stage>/<run_id>/<relative_path>` when that output file exists."""
        clean = relative_path.strip().lstrip("/")
        if not clean:
            return None
        root = self._outputs_root.resolve()
        if not root.is_dir():
            return None
        candidate = (root / self.stage / str(self.run_id) / clean).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        rel = candidate.relative_to(root).as_posix()
        return "/outputs/" + urllib.parse.quote(rel, safe="/")


@dataclass(frozen=True)
class BusyStatus:
    """Live progress snapshot while an interface action or CLI run holds the workspace."""

    label: str
    source: str  # "interface" | "external"
    elapsed_seconds: int
    elapsed_display: str
    short_label: str = ""
    done: int | None = None
    total: int | None = None
    percent: int | None = None
    warning: int | None = None
    quarantined: int | None = None
    remaining_display: str | None = None
    last_activity_seconds: int | None = None
    tab_title_prefix: str = "[Running…]"


@dataclass(frozen=True)
class InterfaceView:
    workspace_root: Path
    recipe_path: Path
    solution_dir: Path
    initialized: bool
    locked: bool
    csrf_token: str
    sources: tuple[SourceInfo, ...]
    role_counts: Mapping[str, int]
    steps: tuple[StepStatus, ...]
    busy: BusyStatus | None = None
    interrupted_step: str | None = None
    _recipe_name: str = field(repr=False, default="")
    _records_by_stage: Mapping[str, tuple[RecordInfo, ...]] = field(
        default_factory=dict, repr=False
    )

    def records(self, stage: str) -> tuple[RecordInfo, ...]:
        """Return current deserialized records for `stage` (short or qualified stage name)."""
        if stage in self._records_by_stage:
            return self._records_by_stage[stage]
        qualified = f"{self._recipe_name}.{stage}"
        return self._records_by_stage.get(qualified, ())


@dataclass(frozen=True)
class StepRunSummary:
    step: str
    status: str
    run_id: int
    valid: int | None = None
    warning: int | None = None
    quarantined: int | None = None
    skipped: int | None = None
    added: int | None = None
    changed: int | None = None
    unchanged: int | None = None
    removed: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class Notice:
    message: str
    is_error: bool = False
    open_pipeline: bool = False


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    file: Path | None = None  # streamed in place of `body` (large originals)


def _content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _response(
    status: int,
    body: bytes,
    content_type: str,
    *,
    cache: str = "no-store",
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    all_headers = {
        "Content-Type": content_type,
        "Cache-Control": cache,
        "X-Content-Type-Options": "nosniff",
        **(headers or {}),
    }
    return HttpResponse(status=status, headers=all_headers, body=body)


def _resolve_under(base: Path, relative: str) -> Path | None:
    """Resolve a URL path to a file inside `base`; `None` if empty, missing, or outside it."""
    clean = urllib.parse.unquote(relative).lstrip("/")
    if not clean or not base.is_dir():
        return None
    root = base.resolve()
    candidate = (root / clean).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _probe_external_lock(workspace_root: Path) -> tuple[bool, str | None]:
    """Non-destructively check if another process holds `workspace.lock` and read its `since=`."""
    lock_path = workspace_root / LOCK_NAME
    if not lock_path.is_file():
        return False, None
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return False, None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raw = os.pread(fd, 200, 0).decode(errors="replace").strip()
            since: str | None = None
            for token in raw.split():
                if token.startswith("since="):
                    since = token.removeprefix("since=").strip() or None
                    break
            return True, since
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False, None
    finally:
        os.close(fd)


def _format_elapsed(seconds: int) -> str:
    clamped = max(0, int(seconds))
    hrs, rem = divmod(clamped, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _seconds_since_iso(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - parsed).total_seconds()))


def _format_snapshot_timestamp(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return ts[:16]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _format_running_step_name(raw_step: str, config_raw: str | None, recipe_name: str) -> str:
    prefix = f"{recipe_name}."
    step = raw_step.removeprefix(prefix) if raw_step.startswith(prefix) else raw_step
    cfg: dict[str, Any] = {}
    if config_raw:
        try:
            loaded = json.loads(config_raw)
            if isinstance(loaded, dict):
                cfg = loaded
        except (ValueError, TypeError):
            cfg = {}
    if step == "parse":
        parser_entry = cfg.get("parser")
        p_val = parser_entry.get("value") if isinstance(parser_entry, dict) else parser_entry
        if isinstance(p_val, str) and p_val.strip():
            return f"parse ({p_val.strip()})"
    elif step == "ingest":
        role_entry = cfg.get("role")
        r_val = role_entry.get("value") if isinstance(role_entry, dict) else role_entry
        if isinstance(r_val, str) and r_val.strip():
            return f"ingest ({r_val.strip()})"
    return step


def _shorten_label(text: str, limit: int = 28) -> str:
    trimmed = text.strip()
    if len(trimmed) > limit:
        return trimmed[: limit - 1].rstrip() + "…"
    return trimmed


def _activity_name(label: str) -> str:
    """Drop a leading Running prefix so the pill can add it back once."""
    text = label.strip()
    for prefix in ("Running step: ", "Running: ", "Running "):
        if text.startswith(prefix):
            rest = text[len(prefix) :].strip()
            return rest or text
    return text


def _tab_prefix_for(short_label: str, done: int | None, total: int | None) -> str:
    trimmed = _shorten_label(short_label, limit=34)
    if done is not None and total is not None:
        return f"[Running: {trimmed} {done}/{total}]"
    if done is not None and done > 0:
        return f"[Running: {trimmed} ({done} done)]"
    if trimmed:
        return f"[Running: {trimmed}]"
    return "[Running…]"


def _inspect_running_ledger(conn: sqlite3.Connection, recipe_name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, step, started_at, config, remaining"
        " FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    step_name = _format_running_step_name(row["step"], row["config"], recipe_name)
    # One query per `/_status` poll: counts per outcome and the latest write.
    grouped = conn.execute(
        "SELECT outcome, count(*) AS n, max(created_at) AS last_ts"
        " FROM outcomes WHERE run_id = ? GROUP BY outcome",
        (row["id"],),
    ).fetchall()
    by_outcome = {str(r["outcome"]): int(r["n"]) for r in grouped}
    done_cnt = sum(by_outcome.values())
    last_ts = max((r["last_ts"] for r in grouped if r["last_ts"]), default=None)
    warning_cnt = by_outcome.get("warning", 0)
    quarantined_cnt = by_outcome.get("quarantined", 0)

    done: int | None = None
    total: int | None = None
    percent: int | None = None
    rem = row["remaining"]
    if isinstance(rem, int) and rem > 0:
        to_do = rem
        if row["config"]:
            try:
                cfg = json.loads(row["config"])
                lim_entry = cfg.get("limit") if isinstance(cfg, dict) else None
                lim_val = lim_entry.get("value") if isinstance(lim_entry, dict) else lim_entry
                if isinstance(lim_val, int) and lim_val > 0:
                    to_do = min(rem, lim_val)
            except (ValueError, TypeError):
                pass
        if to_do >= 2:
            done = min(done_cnt, to_do)
            total = to_do
            percent = min(100, int((done * 100) // total))
    elif done_cnt > 0:
        done = done_cnt

    last_activity = _seconds_since_iso(last_ts or row["started_at"])
    elapsed = _seconds_since_iso(row["started_at"]) or 0
    remaining_display: str | None = None
    if done is not None and total is not None:
        # Skipped outcomes are mostly plan-time skips written at once; leave them out of
        # the rate (same rule as `ProgressSnapshot.skipped`).
        skipped = min(by_outcome.get("skipped", 0), done)
        remaining_display = format_remaining(
            estimate_remaining_seconds(done - skipped, total - skipped, float(elapsed))
        )
    return {
        "step_name": step_name,
        "started_at": row["started_at"],
        "done": done,
        "total": total,
        "percent": percent,
        "warning": warning_cnt,
        "quarantined": quarantined_cnt,
        "remaining_display": remaining_display,
        "last_activity_seconds": last_activity,
    }


def _make_busy_status(
    *,
    source: str,
    in_process_label: str | None,
    in_process_elapsed_seconds: int | None,
    external_since: str | None,
    ledger_info: dict[str, Any] | None,
    in_process_done: int | None = None,
    in_process_total: int | None = None,
) -> BusyStatus:
    warning: int | None = None
    quarantined: int | None = None
    remaining_display: str | None = None
    if ledger_info is not None:
        # A real run's ledger counts win over in-process fetch counters.
        step_name = ledger_info["step_name"]
        label = f"Running: {step_name}"
        tab_name = step_name
        done = ledger_info["done"]
        total = ledger_info["total"]
        percent = ledger_info["percent"]
        warning = ledger_info.get("warning")
        quarantined = ledger_info.get("quarantined")
        remaining_display = ledger_info.get("remaining_display")
        last_act = ledger_info["last_activity_seconds"]
        if in_process_elapsed_seconds is not None:
            elapsed = max(0, in_process_elapsed_seconds)
        else:
            elapsed = _seconds_since_iso(ledger_info["started_at"]) or (
                _seconds_since_iso(external_since) or 0
            )
    else:
        done = None
        total = None
        percent = None
        last_act = None
        if source == "interface":
            label = (in_process_label or "Running: this action").strip()
            tab_name = _activity_name(label)
            elapsed = max(0, in_process_elapsed_seconds or 0)
            # Fetch has no run row: in-process counts fill the bar only when the ledger
            # has nothing running.
            if (
                isinstance(in_process_done, int)
                and isinstance(in_process_total, int)
                and in_process_total >= 2
            ):
                done = max(0, in_process_done)
                total = in_process_total
                percent = min(100, int((done * 100) // total)) if total else None
                remaining_display = format_remaining(
                    estimate_remaining_seconds(done, total, float(elapsed))
                )
        else:
            # No step name yet: the page did not start this work, and the person
            # looking at it only needs to know that some other action is in progress.
            label = "Something else is running"
            tab_name = "another action"
            elapsed = _seconds_since_iso(external_since) or 0

    activity = _shorten_label(tab_name, limit=28) or "working"
    short_label = label if source == "external" and ledger_info is None else f"Running: {activity}"

    return BusyStatus(
        label=label,
        source=source,
        elapsed_seconds=elapsed,
        elapsed_display=_format_elapsed(elapsed),
        short_label=short_label,
        done=done,
        total=total,
        percent=percent,
        warning=warning,
        quarantined=quarantined,
        remaining_display=remaining_display,
        last_activity_seconds=last_act,
        tab_title_prefix=_tab_prefix_for(tab_name, done, total),
    )


def _required_parse_steps(recipe: Recipe) -> tuple[bool, list[str]]:
    """Return `(needs_default_parse, extra_parsers)` derived from `Stage.text`."""
    needs_default = any(s.text is not None for s in recipe.stages.values())
    extra: list[str] = []
    for s in recipe.stages.values():
        for name in text_parsers(s.text):
            if name != "default" and name not in extra:
                extra.append(name)
    return needs_default, extra


_COUNTED = ("valid", "warning", "quarantined", "skipped")


def _count_summary(
    valid: int | None,
    warning: int | None,
    quarantined: int | None,
    skipped: int | None,
) -> str:
    parts: list[str] = []
    if valid:
        parts.append(f"{valid} valid")
    if warning:
        parts.append(f"{warning} warning")
    if quarantined:
        parts.append(f"{quarantined} quarantined")
    if skipped:
        parts.append(f"{skipped} skipped")
    return ", ".join(parts) if parts else "0 subjects"


def _default_parse_key(locator: str) -> tuple[str, str] | None:
    """`(stage, stage_fp)` of the default parser for `locator`, or `None` if it has none."""
    p_key = DEFAULT_FOR.get(extension(locator))
    if not p_key:
        return None
    parser = PARSERS[p_key]
    return f"parse.{parser.name}", parser.version


ParseDoneMap = dict[tuple[str, int, str], tuple[str, int]]


def _parse_done_map(conn: sqlite3.Connection, limits: Limits | None) -> ParseDoneMap:
    """Latest parse `(outcome, run_id)` per `(stage, item_version_id, stage_fp)`.

    A limit quarantine that the current limits make stale counts as not done.
    """
    done: ParseDoneMap = {}
    for row in conn.execute(
        "SELECT run_id, stage, item_version_id, stage_fp, outcome, reason_code, diagnostics"
        " FROM outcomes WHERE stage = 'parse' OR stage LIKE 'parse.%' ORDER BY id ASC"
    ):
        key = (row["stage"], row["item_version_id"], row["stage_fp"])
        if is_limit_quarantine_stale(
            row["stage"], row["outcome"], row["reason_code"], row["diagnostics"], limits
        ):
            done.pop(key, None)
        else:
            done[key] = (row["outcome"], int(row["run_id"]))
    return done


def _compute_parse_status(
    current_items: Sequence[sqlite3.Row],
    done_map: ParseDoneMap,
    *,
    order: int,
    parser_name: str | None = None,
) -> StepStatus:
    mk = partial(
        StepStatus,
        key=f"parse:{parser_name}" if parser_name else "parse",
        label=f"parse ({parser_name})" if parser_name else "parse",
        kind="parse",
        order=order,
    )
    if parser_name is not None:
        try:
            PARSERS[parser_name].check()
        except ParserUnavailable:
            return mk(
                status="unavailable",
                summary=f"{parser_name} parser unavailable (local binary not installed)",
                runnable=False,
            )
    if not current_items:
        return mk(status="blocked", summary="Waiting for ingest")

    scope_items = current_items
    if parser_name is not None:
        exts = PARSERS[parser_name].extensions
        scope_items = [ci for ci in current_items if extension(ci["locator"]) in exts]
        if not scope_items:
            return mk(status="current", summary=f"No {', '.join(sorted(exts))} items in workspace")

    counts: Counter[str] = Counter()
    pending = 0
    needs_ocr_count = 0
    for ci in scope_items:
        iv_id = ci["item_version_id"]
        if parser_name is None:
            st_name, st_fp = _default_parse_key(ci["locator"]) or ("parse", "")
        else:
            st_name, st_fp = f"parse.{PARSERS[parser_name].name}", PARSERS[parser_name].version
        found = done_map.get((st_name, iv_id, st_fp))
        if found is not None:
            counts[found[0]] += 1
            continue
        pending += 1
        if parser_name == "ocr":
            def_key = _default_parse_key(ci["locator"])
            def_found = done_map.get((def_key[0], iv_id, def_key[1])) if def_key else None
            if def_found is not None and def_found[0] in ("warning", "quarantined"):
                needs_ocr_count += 1

    count_kw = {o: counts[o] for o in _COUNTED}
    total_done = sum(counts.values())
    if parser_name == "ocr" and pending > 0 and needs_ocr_count == 0:
        # All pending PDFs parsed cleanly via their native text layer (or await default parse).
        summary = (
            _count_summary(**count_kw)
            if total_done > 0
            else "No unreadable PDF pages requiring OCR"
        )
        return mk(status="current", summary=summary, **count_kw)

    if parser_name == "ocr" and needs_ocr_count > 0:
        return mk(
            status="pending" if total_done > 0 else "not_run",
            summary=f"{needs_ocr_count} PDF(s) with unreadable pages need OCR",
            pending=needs_ocr_count,
            **count_kw,
        )

    if pending > 0 and total_done == 0:
        status = "not_run"
        summary = f"{pending} item(s) ready to parse"
    elif pending > 0:
        status = "pending"
        summary = f"{pending} item(s) pending; " + _count_summary(**count_kw)
    elif (
        parser_name is None
        and counts["valid"] == 0
        and counts["warning"] == 0
        and counts["quarantined"] > 0
    ):
        status = "failed"
        summary = f"All {counts['quarantined']} parsed item(s) quarantined"
    else:
        # Opt-in recovery parsers (such as `ocr`) skip clean PDFs and attempt only items with
        # unreadable pages; once every item has a recorded outcome (`pending == 0`), a rerun
        # would be a cache `noop` even if the sole attempted scan yielded no text.
        status = "current"
        summary = _count_summary(**count_kw)
    return mk(status=status, summary=summary, pending=pending, **count_kw)


def _parse_steps(
    recipe: Recipe, current_items: Sequence[sqlite3.Row], done_map: ParseDoneMap
) -> list[StepStatus]:
    """Default parse (when any stage reads text), then each extra parser, from order 2."""
    needs_default, extra_parsers = _required_parse_steps(recipe)
    names: list[str | None] = [None] if needs_default else []
    names.extend(extra_parsers)
    return [
        _compute_parse_status(current_items, done_map, order=2 + i, parser_name=name)
        for i, name in enumerate(names)
    ]


def _uninitialized_steps(recipe: Recipe) -> tuple[StepStatus, ...]:
    steps = [
        StepStatus(
            key="ingest",
            label="ingest",
            kind="ingest",
            order=1,
            status="not_run",
            summary="No items ingested yet",
            runnable=False,
        ),
        *_parse_steps(recipe, [], {}),
    ]
    for s_name, stage in recipe.stages.items():
        steps.append(
            StepStatus(
                key=s_name,
                label=s_name,
                kind="recipe",
                order=len(steps) + 1,
                status="not_run" if stage.allow_empty else "blocked",
                summary="Ready (empty workspace allowed)"
                if stage.allow_empty
                else "Waiting for ingest",
                needs=stage.needs,
                roles=stage.roles,
                uses_llm=bool(stage.params.get("model")),
            )
        )
    return tuple(steps)


def _compute_stage_status(
    conn: sqlite3.Connection,
    recipe: Recipe,
    stage: Stage,
    current_items: list[sqlite3.Row],
    outputs_root: Path,
    usable_record_counts: dict[str, int],
    latest_stage_runs: dict[str, int],
    *,
    order: int,
) -> tuple[StepStatus, tuple[RecordInfo, ...]]:
    full = recipe.stage_name(stage.name)
    current_fp = recipe.stage_fps[stage.name]
    scope_items = [ci for ci in current_items if stage.roles is None or ci["role"] in stage.roles]

    raw_records = conn.execute(
        "SELECT cr.*, o.stage_fp AS outcome_stage_fp FROM current_records cr"
        " JOIN outcomes o ON o.id = cr.outcome_id WHERE cr.stage = ? ORDER BY cr.subject_key",
        (full,),
    ).fetchall()
    records: list[RecordInfo] = []
    schema_mismatch = False
    usable_count = 0
    for rec in raw_records:
        try:
            payload = stage.schema.model_validate_json(rec["payload"])
        except ValidationError:
            schema_mismatch = True
            continue
        reasons = [
            r
            for r in ("fallback", "version_stale", "code_stale", "params_stale", "input_stale")
            if rec[r]
        ]
        # The recorded stage_fp differing from the loaded recipe means code changed since.
        if rec["outcome_stage_fp"] != current_fp and "code_stale" not in reasons:
            reasons.append("code_stale")
        if not rec["fallback"]:
            usable_count += 1
        records.append(
            RecordInfo(
                stage=full,
                subject_kind=rec["subject_kind"],
                subject_key=rec["subject_key"],
                run_id=rec["run_id"],
                outcome=rec["outcome"],
                reason_code=rec["reason_code"],
                payload=payload,
                stale=bool(reasons),
                stale_reasons=tuple(reasons),
                _outputs_root=outputs_root,
            )
        )
    usable_record_counts[stage.name] = usable_count
    recs = tuple(records)

    outcomes_rows = conn.execute(
        "SELECT * FROM current_outcomes WHERE stage = ?", (full,)
    ).fetchall()
    latest_run = conn.execute(
        "SELECT id, status, error, remaining FROM runs WHERE step = ? ORDER BY id DESC LIMIT 1",
        (full,),
    ).fetchone()
    latest_completed = conn.execute(
        "SELECT max(id) AS max_id FROM runs WHERE step = ? AND remaining = 0"
        " AND status IN ('succeeded', 'noop')",
        (full,),
    ).fetchone()
    completed_run_id = (
        int(latest_completed["max_id"])
        if latest_completed and latest_completed["max_id"] is not None
        else 0
    )
    latest_stage_runs[stage.name] = completed_run_id

    counts: Counter[str] = Counter(r["outcome"] for r in outcomes_rows)
    count_kw = {o: counts[o] for o in _COUNTED}
    mk = partial(
        StepStatus,
        key=stage.name,
        label=stage.name,
        kind="recipe",
        order=order,
        needs=stage.needs,
        roles=stage.roles,
        uses_llm=bool(stage.params.get("model")),
    )

    missing_needs = [n for n in stage.needs if usable_record_counts.get(n, 0) == 0]
    if scope_items and missing_needs:
        return mk(status="blocked", summary=f"Run {', '.join(missing_needs)} first"), recs

    if not scope_items and not stage.allow_empty:
        role_note = f" ({', '.join(stage.roles)})" if stage.roles else ""
        return mk(status="blocked", summary=f"Waiting for ingested items{role_note}"), recs

    if scope_items and stage.text is not None:
        parsed_ids = {
            r[0] for r in conn.execute("SELECT DISTINCT item_version_id FROM current_text")
        }
        parseable = [ci for ci in scope_items if parsers_for(stage.text, ci["locator"])]
        if parseable and not any(ci["item_version_id"] in parsed_ids for ci in parseable):
            return mk(status="blocked", summary="Run parse first"), recs

    if latest_run and latest_run["status"] == "failed" and latest_run["id"] > completed_run_id:
        err = (latest_run["error"] or "last run failed").splitlines()[0][:140]
        return (
            mk(status="failed", summary=f"Failed: {err}", error=latest_run["error"], **count_kw),
            recs,
        )

    if not outcomes_rows and completed_run_id == 0:
        summary = f"{len(scope_items)} item(s) in scope" if scope_items else "Not yet run"
        return mk(status="not_run", summary=summary), recs

    stale_count = sum(
        1
        for r in outcomes_rows
        if r["version_stale"]
        or r["code_stale"]
        or r["params_stale"]
        or r["input_stale"]
        or r["stage_fp"] != current_fp
    )
    if schema_mismatch:
        stale_count = max(stale_count, 1)

    pending_count = 0
    if stage.subject == "item":
        seen_keys = {r["subject_key"] for r in outcomes_rows}
        pending_count = sum(1 for ci in scope_items if f"item:{ci['item_id']}" not in seen_keys)
    else:
        if any(ci["run_id"] > completed_run_id for ci in scope_items):
            pending_count += 1
        if any(latest_stage_runs.get(n, 0) > completed_run_id for n in stage.needs):
            pending_count += 1

    if latest_run and (latest_run["remaining"] or 0) > 0:
        pending_count = max(pending_count, int(latest_run["remaining"]))

    base_summary = _count_summary(**count_kw)
    if pending_count > 0:
        status = "pending"
        summary = f"New or unfinished inputs since last run ({base_summary})"
    elif stale_count > 0:
        status = "stale"
        summary = f"Inputs or stage code changed ({base_summary})"
    else:
        status = "current"
        summary = base_summary
    return (
        mk(status=status, summary=summary, pending=pending_count, stale=stale_count, **count_kw),
        recs,
    )


def _open_current(db_path: Path) -> sqlite3.Connection:
    """Open `state.db` read-only, refusing a schema this code does not expect."""
    conn = open_readonly(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != len(MIGRATIONS):
        conn.close()
        raise WorkspaceError(
            f"{db_path.parent}: schema version {version}, this code expects {len(MIGRATIONS)}; "
            "run a stage once to migrate"
        )
    return conn


def build_view(
    workspace_root: Path,
    recipe: Recipe,
    csrf_token: str,
    *,
    in_process_busy: bool = False,
    in_process_label: str | None = None,
    in_process_elapsed_seconds: int | None = None,
    in_process_done: int | None = None,
    in_process_total: int | None = None,
) -> InterfaceView:
    """Inspect `workspace_root` read-only and assemble `InterfaceView` (writes nothing)."""
    ws_root = workspace_root.resolve()
    solution_dir = recipe.path.parent
    ext_locked, ext_since = (False, None) if in_process_busy else _probe_external_lock(ws_root)
    locked = in_process_busy or ext_locked
    busy_source = "interface" if in_process_busy else "external"
    db_path = ws_root / DB_NAME
    if not db_path.is_file():
        busy = (
            _make_busy_status(
                source=busy_source,
                in_process_label=in_process_label,
                in_process_elapsed_seconds=in_process_elapsed_seconds,
                external_since=ext_since,
                ledger_info=None,
                in_process_done=in_process_done,
                in_process_total=in_process_total,
            )
            if locked
            else None
        )
        return InterfaceView(
            workspace_root=ws_root,
            recipe_path=recipe.path,
            solution_dir=solution_dir,
            initialized=False,
            locked=locked,
            csrf_token=csrf_token,
            sources=(),
            role_counts=MappingProxyType({}),
            steps=_uninitialized_steps(recipe),
            busy=busy,
            interrupted_step=None,
            _recipe_name=recipe.name,
            _records_by_stage=MappingProxyType({}),
        )

    conn = _open_current(db_path)
    try:
        ledger_running = _inspect_running_ledger(conn, recipe.name)
        interrupted_step: str | None = None
        if not locked and ledger_running is not None:
            # Re-probe once after seeing `status = 'running'` so a CLI process that acquired
            # `workspace.lock` mid-render is treated as live rather than interrupted.
            ext_locked_recheck, ext_since_recheck = _probe_external_lock(ws_root)
            if ext_locked_recheck:
                locked = True
                ext_since = ext_since_recheck
                busy_source = "external"
            else:
                interrupted_step = ledger_running["step_name"]

        busy = (
            _make_busy_status(
                source=busy_source,
                in_process_label=in_process_label,
                in_process_elapsed_seconds=in_process_elapsed_seconds,
                external_since=ext_since,
                ledger_info=ledger_running,
                in_process_done=in_process_done,
                in_process_total=in_process_total,
            )
            if locked
            else None
        )

        try:
            resolved_limits, _ = resolve_limits(recipe.limits)
        except ValueError:
            resolved_limits = recipe.limits

        source_rows = conn.execute(
            "SELECT s.id, s.kind, s.uri, s.role, s.created_at,"
            " (SELECT count(*) FROM current_items ci WHERE ci.source_id = s.id) AS item_count,"
            " (SELECT r.discovered FROM runs r"
            "  WHERE r.step = 'ingest' AND r.source_id = s.id"
            "  AND r.status IN ('succeeded', 'noop') ORDER BY r.id DESC LIMIT 1)"
            "  AS last_discovered_count,"
            " COALESCE((SELECT max(v.run_id) FROM items i"
            "  JOIN item_versions v ON v.item_id = i.id AND v.is_current = 1"
            "  WHERE i.source_id = s.id), 0) AS latest_item_run_id,"
            " COALESCE((SELECT max(ct.run_id) FROM current_items ci"
            "  JOIN current_text ct ON ct.item_version_id = ci.item_version_id"
            "  WHERE ci.source_id = s.id), 0) AS latest_text_run_id"
            " FROM sources s ORDER BY s.id"
        ).fetchall()
        current_items = conn.execute(
            "SELECT item_id, item_version_id, source_id, locator, role, run_id"
            " FROM current_items ORDER BY item_version_id"
        ).fetchall()

        needs_default, extra_parsers = _required_parse_steps(recipe)
        available_extra: list[str] = []
        for p_name in extra_parsers:
            try:
                PARSERS[p_name].check()
                available_extra.append(p_name)
            except ParserUnavailable:
                pass

        done_map: ParseDoneMap = {}
        unparsed_by_source: Counter[int] = Counter()
        latest_parse_by_source: dict[int, int] = {
            r["id"]: int(r["latest_text_run_id"]) for r in source_rows
        }
        if current_items and (needs_default or available_extra):
            done_map = _parse_done_map(conn, resolved_limits)
            for ci in current_items:
                sid = ci["source_id"]
                iv_id = ci["item_version_id"]
                ext = extension(ci["locator"])
                def_out: str | None = None
                if needs_default:
                    st_name, st_fp = _default_parse_key(ci["locator"]) or ("parse", "")
                    found = done_map.get((st_name, iv_id, st_fp))
                    if found is None:
                        unparsed_by_source[sid] += 1
                    else:
                        def_out, def_run = found
                        if def_out != "skipped":
                            latest_parse_by_source[sid] = max(
                                latest_parse_by_source.get(sid, 0), def_run
                            )
                for p_name in available_extra:
                    p_obj = PARSERS[p_name]
                    if ext not in p_obj.extensions:
                        continue
                    ext_found = done_map.get((f"parse.{p_obj.name}", iv_id, p_obj.version))
                    if (
                        ext_found is None
                        and p_name == "ocr"
                        and def_out in ("warning", "quarantined")
                    ):
                        unparsed_by_source[sid] += 1
                    elif ext_found is not None and ext_found[0] in ("valid", "warning"):
                        latest_parse_by_source[sid] = max(
                            latest_parse_by_source.get(sid, 0), ext_found[1]
                        )

        sources = tuple(
            SourceInfo(
                id=r["id"],
                kind=r["kind"],
                uri=r["uri"],
                role=r["role"],
                created_at=r["created_at"],
                item_count=r["item_count"],
                last_discovered_count=r["last_discovered_count"],
                latest_item_run_id=int(r["latest_item_run_id"]),
                latest_parse_run_id=latest_parse_by_source.get(r["id"], 0),
                unparsed_item_count=unparsed_by_source.get(r["id"], 0),
            )
            for r in source_rows
        )
        role_counter = Counter(r["role"] for r in current_items)

        if current_items:
            roles_str = ", ".join(f"{role}: {cnt}" for role, cnt in sorted(role_counter.items()))
            ingest_summary = (
                f"{len(current_items)} item(s) across {len(sources)} source(s) ({roles_str})"
            )
            ingest_status = "current"
        else:
            ingest_summary = "No current items ingested"
            ingest_status = "not_run"
        steps = [
            StepStatus(
                key="ingest",
                label="ingest",
                kind="ingest",
                order=1,
                status=ingest_status,
                summary=ingest_summary,
                runnable=False,
            ),
            *_parse_steps(recipe, current_items, done_map),
        ]
        order = len(steps) + 1

        outputs_root = ws_root / OUTPUTS_DIR
        records_by_stage: dict[str, tuple[RecordInfo, ...]] = {}
        usable_counts: dict[str, int] = {}
        latest_stage_runs: dict[str, int] = {}
        for s_name, stage in recipe.stages.items():
            st, recs = _compute_stage_status(
                conn,
                recipe,
                stage,
                current_items,
                outputs_root,
                usable_counts,
                latest_stage_runs,
                order=order,
            )
            steps.append(st)
            records_by_stage[s_name] = recs
            records_by_stage[recipe.stage_name(s_name)] = recs
            order += 1

        if current_items:
            for idx, st in enumerate(steps):
                if st.runnable and st.status in ("not_run", "pending", "stale", "failed"):
                    steps[idx] = StepStatus(
                        **{**st.__dict__, "recommended_next": True},
                    )
                    break

        return InterfaceView(
            workspace_root=ws_root,
            recipe_path=recipe.path,
            solution_dir=solution_dir,
            initialized=True,
            locked=locked,
            csrf_token=csrf_token,
            sources=sources,
            role_counts=MappingProxyType(dict(sorted(role_counter.items()))),
            steps=tuple(steps),
            busy=busy,
            interrupted_step=interrupted_step,
            _recipe_name=recipe.name,
            _records_by_stage=MappingProxyType(records_by_stage),
        )
    finally:
        conn.close()


class ActionContext:
    """In-process execution context passed to `Interface.actions` handlers."""

    def __init__(
        self,
        workspace_root: Path,
        recipe: Recipe,
        *,
        trigger: str = "interface",
        workers: int | None = None,
        status_callback: Callable[..., None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.recipe = recipe
        self.solution_dir = recipe.path.parent
        self.trigger = trigger
        self.workers = _DEFAULT_WORKERS if workers is None else workers
        self._explicit_workers = workers
        self._status_callback = status_callback
        self._cancel_event = cancel_event

    def set_status(
        self,
        label: str,
        *,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        """Update the live phase label (and optional done/total) in the busy banner."""
        cleaned = label.strip()
        if cleaned and self._status_callback is not None:
            self._status_callback(cleaned, done=done, total=total)

    def ingest(
        self,
        source: Path,
        *,
        role: str = "corpus",
        ignore: Sequence[str] | None = None,
        no_default_ignore: bool = False,
        allow_empty: bool = False,
    ) -> StepRunSummary:
        """Ingest `source` into the workspace under `lock(workspace_root)`."""
        if not role.strip():
            raise ActionError("role must not be empty")
        try:
            limits, limits_cfg = resolve_limits(self.recipe.limits)
        except ValueError as exc:
            raise ActionError(str(exc)) from None
        self.set_status(f"Running: ingest ({role})")
        resolved_ignore = effective_ignore(ignore, no_default_ignore=no_default_ignore)
        config = {
            "step": {"value": "ingest", "from": "interface"},
            "workspace": {"value": str(self.workspace_root), "from": "interface"},
            "source": {"value": str(source), "from": "interface"},
            "role": {"value": role, "from": "interface"},
            "trigger": {"value": self.trigger, "from": "interface"},
            "effective_ignore": list(resolved_ignore),
            "limits": limits_cfg,
        }
        try:
            check_folder_source(source, self.workspace_root)
            with lock(self.workspace_root):
                ws = open_workspace(self.workspace_root)
                try:
                    reap_abandoned(ws.conn)
                    result = ingest_folder(
                        ws,
                        source,
                        role=role,
                        trigger=self.trigger,
                        config=config,
                        allow_empty=allow_empty,
                        ignore=resolved_ignore,
                        limits=limits,
                        cancel_event=self._cancel_event,
                    )
                finally:
                    ws.close()
        except WorkspaceLocked as exc:
            raise ActionError(str(exc)) from None
        except (WorkspaceError, ValueError, RunCancelled) as exc:
            raise ActionError(str(exc)) from None

        summary = StepRunSummary(
            step=f"ingest ({role})",
            status=result.status,
            run_id=result.run_id,
            quarantined=result.quarantined,
            added=result.added,
            changed=result.changed,
            unchanged=result.unchanged,
            removed=result.removed,
            error=result.error,
        )
        if result.status == "failed":
            raise ActionError(f"ingest ({role}) failed: {result.error or 'run failed'}")
        return summary

    def run_step(
        self,
        step: str,
        *,
        allow_empty: bool = False,
        allow_all_quarantined: bool = False,
        fresh: bool = False,
        no_cache: bool = False,
        params: Mapping[str, Any] | None = None,
    ) -> StepRunSummary:
        """Run `parse`, `parse:<parser>`, or one recipe stage under `lock(workspace_root)`."""
        parser_name: str | None = None
        is_parse = False
        llm_target = None
        if step == "parse":
            is_parse = True
        elif step.startswith("parse:"):
            is_parse = True
            parser_name = step.split(":", 1)[1]
            if parser_name not in PARSERS:
                raise ActionError(f"unknown parser {parser_name!r}")
            try:
                PARSERS[parser_name].check()
            except ParserUnavailable as exc:
                raise ActionError(str(exc)) from None
        else:
            if step not in self.recipe.stages:
                raise ActionError(f"recipe {self.recipe.name} has no stage {step!r}")
            try:
                llm_target = validate_stage_llm(
                    {**self.recipe.stages[step].params, **(params or {})}
                )
            except ValueError as exc:
                raise ActionError(str(exc)) from None

        try:
            limits, limits_cfg = resolve_limits(self.recipe.limits)
        except ValueError as exc:
            raise ActionError(str(exc)) from None

        local_llm_default_workers = (
            not is_parse
            and llm_target is not None
            and llm_target.is_local
            and self._explicit_workers is None
        )
        step_workers = 1 if local_llm_default_workers else self.workers

        config: dict[str, Any] = {
            "step": {"value": "parse" if is_parse else step, "from": "interface"},
            "workspace": {"value": str(self.workspace_root), "from": "interface"},
            "trigger": {"value": self.trigger, "from": "interface"},
            "workers": {
                "value": step_workers,
                "from": "local-llm-default" if local_llm_default_workers else "interface",
            },
            "limits": limits_cfg,
        }
        if is_parse and parser_name is not None:
            config["parser"] = {"value": parser_name, "from": "interface"}
        if not is_parse:
            config["recipe"] = {"value": str(self.recipe.path), "from": "interface"}
            config["recipe_fp"] = self.recipe.fingerprint

        display_step = f"parse ({parser_name})" if parser_name else step
        self.set_status(f"Running: {display_step}")
        try:
            with lock(self.workspace_root):
                ws = open_workspace(self.workspace_root)
                try:
                    reap_abandoned(ws.conn)
                    if is_parse:
                        plan = parse_plan(
                            ws,
                            parser_name,
                            allow_empty=allow_empty,
                            limits=limits,
                            workers=step_workers,
                        )
                    else:
                        overrides = dict(params or {})
                        effective = {**self.recipe.stages[step].params, **overrides}
                        register_stages(ws.conn, self.recipe, param_overrides={step: effective})
                        plan = recipe_plan(
                            ws,
                            self.recipe,
                            step,
                            allow_empty=allow_empty,
                            params=overrides,
                        )
                    result = run_plan(
                        ws,
                        plan,
                        trigger=self.trigger,
                        config=config,
                        workers=step_workers,
                        fresh=fresh,
                        no_cache=no_cache,
                        cancel_event=self._cancel_event,
                    )
                finally:
                    ws.close()
        except WorkspaceLocked as exc:
            raise ActionError(str(exc)) from None
        except (WorkspaceError, ValueError, RunCancelled) as exc:
            raise ActionError(str(exc)) from None

        summary = StepRunSummary(
            step=step,
            status=result.status,
            run_id=result.run_id,
            valid=result.valid,
            warning=result.warning,
            quarantined=result.quarantined,
            skipped=result.skipped,
            error=result.error,
        )
        if result.status == "failed":
            if (
                allow_all_quarantined
                and result.error is not None
                and result.error.startswith("AllQuarantined:")
            ):
                return summary
            completed = (
                result.unchanged
                + result.valid
                + result.warning
                + result.quarantined
                + result.skipped
            )
            rem = result.remaining or 0
            progress_note = (
                f" (run #{result.run_id}; {completed} completed, {rem} remaining — rerun to resume)"
                if completed > 0 and rem > 0
                else f" (run #{result.run_id})"
            )
            raise ActionError(f"step {step} failed: {result.error or 'run failed'}{progress_note}")
        return summary

    def _workspace_role_counts(self) -> tuple[int, dict[str, int], bool]:
        """Return `(total_items, role_counts, any_pdf_needs_ocr)` read-only."""
        db_path = self.workspace_root / DB_NAME
        if not db_path.is_file():
            return 0, {}, False
        conn = open_readonly(db_path)
        try:
            rows = conn.execute(
                "SELECT item_version_id, locator, role FROM current_items"
            ).fetchall()
            roles = Counter(r["role"] for r in rows)
            bad_pdfs = conn.execute(
                "SELECT 1 FROM outcomes o"
                " JOIN current_items ci ON ci.item_version_id = o.item_version_id"
                " WHERE o.stage = 'parse.pdfplumber'"
                " AND o.outcome IN ('warning', 'quarantined') LIMIT 1"
            ).fetchone()
            needs_ocr = bad_pdfs is not None
            return len(rows), dict(roles), needs_ocr
        finally:
            conn.close()

    def run_pipeline(
        self,
        *,
        fresh: bool = False,
        no_cache: bool = False,
    ) -> list[StepRunSummary]:
        """Run required parse steps and every recipe stage in declared topological order."""
        total_items, role_counts, _ = self._workspace_role_counts()
        all_allow_empty = all(s.allow_empty for s in self.recipe.stages.values())
        if total_items == 0 and not all_allow_empty:
            raise ActionError("No items are ingested in the workspace yet.")

        results: list[StepRunSummary] = []
        needs_default, extra_parsers = _required_parse_steps(self.recipe)
        if needs_default:
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise ActionError("pipeline interrupted")
            results.append(self.run_step("parse", allow_empty=(total_items == 0)))

        total_items, role_counts, needs_ocr = self._workspace_role_counts()
        for p_name in extra_parsers:
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise ActionError("pipeline interrupted")
            try:
                PARSERS[p_name].check()
            except ParserUnavailable:
                continue
            if p_name == "ocr" and not needs_ocr:
                continue
            # Recovery parsers (such as `ocr`) skip items that already parsed cleanly and only
            # attempt unreadable ones; if the sole unreadable attachment is a blank scan with zero
            # OCR text, let downstream stages handle its quarantine rather than aborting the run.
            results.append(
                self.run_step(
                    f"parse:{p_name}",
                    allow_empty=True,
                    allow_all_quarantined=True,
                )
            )

        for s_name, stage in self.recipe.stages.items():
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise ActionError("pipeline interrupted")
            in_scope = (
                total_items
                if stage.roles is None
                else sum(role_counts.get(r, 0) for r in stage.roles)
            )
            # When the workspace has items in other roles (e.g. a docket with documents but zero
            # comments yet), permit a role-filtered stage with zero matching items to pass as a
            # clean noop rather than halting downstream deliverables.
            permit_empty = stage.allow_empty or (total_items > 0 and in_scope == 0)
            results.append(
                self.run_step(
                    s_name,
                    allow_empty=permit_empty,
                    fresh=fresh,
                    no_cache=no_cache,
                )
            )
        return results


class InterfaceApp:
    """In-process HTTP application mounting one recipe's `INTERFACE` over one workspace."""

    def __init__(
        self,
        workspace_root: Path,
        recipe_path: Path,
        *,
        htmx_wait_seconds: float = 0.25,
    ) -> None:
        self.workspace_root = check_root(workspace_root)
        self.recipe_path = recipe_path.resolve()
        loaded = load_recipe(self.recipe_path)
        if loaded.interface is None:
            raise RecipeError(f"recipe {loaded.name} defines no INTERFACE")
        self._recipe_cache_lock = threading.Lock()
        self._cached_recipe: Recipe = loaded
        self._cached_recipe_fp: str = loaded.fingerprint
        self.csrf_token = secrets.token_urlsafe(32)
        self._htmx_wait_seconds = max(0.0, float(htmx_wait_seconds))
        self._run_mutex = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_label: str | None = None
        self._active_done: int | None = None
        self._active_total: int | None = None
        self._active_started_mono: float | None = None
        self._active_done_event: threading.Event | None = None
        self._active_cancel_event: threading.Event | None = None
        self._completed_notice: Notice | None = None
        self._notices_lock = threading.Lock()
        self._notices: dict[str, Notice] = {}
        self._shell_env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml", "j2"]),
        )
        self._review = ReviewPages(self)

    def _set_active_label(
        self,
        label: str,
        *,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        with self._state_lock:
            if self._active_started_mono is not None:
                # Counts belong to the label they came with: a new phase without counts
                # clears them rather than showing the previous phase's done/total.
                self._active_label = label
                self._active_done = done
                self._active_total = total

    def _snapshot_in_process_activity(
        self,
    ) -> tuple[bool, str | None, int | None, int | None, int | None]:
        with self._state_lock:
            busy = self._run_mutex.locked() or self._active_started_mono is not None
            if not busy:
                return False, None, None, None, None
            elapsed = (
                max(0, int(time.monotonic() - self._active_started_mono))
                if self._active_started_mono is not None
                else 0
            )
            return True, self._active_label, elapsed, self._active_done, self._active_total

    def _pop_completed_notice(self) -> Notice | None:
        with self._state_lock:
            notice = self._completed_notice
            self._completed_notice = None
            return notice

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        """Block until any active background interface action finishes (or `timeout` expires)."""
        with self._state_lock:
            ev = self._active_done_event
        if ev is None:
            return True
        return ev.wait(timeout=timeout)

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Signal any active run to cancel, reap child parse workers, and wait for clean exit."""
        with self._state_lock:
            cancel_ev = self._active_cancel_event
        if cancel_ev is not None:
            cancel_ev.set()
        return self.wait_for_idle(timeout=timeout)

    def _load_current_recipe(self) -> Recipe:
        try:
            current_fp = recipe_fingerprint(self.recipe_path)
        except OSError as exc:
            raise RecipeError(str(exc)) from exc
        with self._recipe_cache_lock:
            if self._cached_recipe_fp == current_fp:
                return self._cached_recipe
            loaded = load_recipe(self.recipe_path)
            if loaded.interface is None:
                raise RecipeError(f"recipe {loaded.name} defines no INTERFACE")
            self._cached_recipe = loaded
            self._cached_recipe_fp = loaded.fingerprint
            return loaded

    def _brand_dir(self, recipe: Recipe, interface: Interface) -> Path | None:
        if interface.brand_dir is False:
            return None
        rel = interface.brand_dir if isinstance(interface.brand_dir, str) else "brand"
        if not rel:
            return None
        candidate = (recipe.path / rel).resolve()
        if candidate.is_relative_to(recipe.path) and candidate.is_dir():
            return candidate
        return None

    def _brand_css_dir(self, brand_dir: Path | None) -> Path | None:
        """`brand_dir` when it holds a non-empty `brand.css` (scanned for external URLs)."""
        css = brand_dir / "brand.css" if brand_dir is not None else None
        if css is not None and css.is_file() and css.read_text(encoding="utf-8").strip():
            return brand_dir
        return None

    def _html_response(
        self,
        html: str,
        brand_css_dir: Path | None,
        what: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Serve `html` only if it references no external resource (offline invariant)."""
        ext_hits = page_external_resource_count(html, brand_css_dir)
        if ext_hits > 0:
            return self._text_error(
                500, f"refusing to serve {what} with {ext_hits} external resource URL(s)"
            )
        return _response(200, html.encode("utf-8"), "text/html; charset=utf-8", headers=headers)

    def _resolve_footer_html(self, brand_dir: Path | None, interface: Interface) -> Markup:
        if interface.footer is not None and interface.footer.strip():
            lines = [
                escape(line.strip())
                for line in interface.footer.strip().splitlines()
                if line.strip()
            ]
            return Markup("<p>") + Markup("<br>").join(lines) + Markup("</p>")
        brand_frag = _fragment(brand_dir, "footer.html")
        if brand_frag:
            return brand_frag
        return DEFAULT_INTERFACE_FOOTER

    def _store_notice(self, notice: Notice) -> str:
        token = secrets.token_urlsafe(12)
        with self._notices_lock:
            if len(self._notices) >= 32:
                oldest = next(iter(self._notices))
                del self._notices[oldest]
            self._notices[token] = notice
        return token

    def _pop_notice(self, token: str | None) -> Notice | None:
        if not token:
            return None
        with self._notices_lock:
            return self._notices.pop(token, None)

    def _pipeline_health(self, view: InterfaceView) -> tuple[str, str]:
        if not view.initialized:
            return ("not initialized", "neutral")
        runnable = [s for s in view.steps if s.runnable]
        if not runnable:
            return ("ready", "ok")
        counts = Counter(s.status for s in runnable)
        if counts["failed"]:
            return (f"{counts['failed']} failed", "error")
        attention = counts["not_run"] + counts["pending"] + counts["stale"]
        if attention and sum(view.role_counts.values()) > 0:
            return (f"{attention} step(s) need attention", "warn")
        if all(s.status == "current" for s in runnable):
            return ("all current", "ok")
        return (f"{counts['current']}/{len(runnable)} current", "neutral")

    def _current_view(self, recipe: Recipe) -> InterfaceView:
        in_proc_busy, in_proc_label, in_proc_elapsed, in_proc_done, in_proc_total = (
            self._snapshot_in_process_activity()
        )
        return build_view(
            self.workspace_root,
            recipe,
            self.csrf_token,
            in_process_busy=in_proc_busy,
            in_process_label=in_proc_label,
            in_process_elapsed_seconds=in_proc_elapsed,
            in_process_done=in_proc_done,
            in_process_total=in_proc_total,
        )

    def _render_html(
        self,
        recipe: Recipe,
        *,
        partial: bool,
        notice: Notice | None = None,
        prebuilt_view: InterfaceView | None = None,
        extra_headers: Mapping[str, str] | None = None,
        body: Markup | None = None,
        review_mode: bool = False,
    ) -> HttpResponse:
        """The recipe's interface page, or the core chrome around `body` (review pages).

        `review_mode` drops the polling busy bar and the steps drawer: when a run ends,
        the poll swaps the whole page for the home view, which would discard a reviewer's
        half-written verdict. Run status is shown without polling instead.
        """
        interface = recipe.interface
        assert interface is not None
        if review_mode:
            assert body is not None
            view: Any = {"busy": self._poll_busy_status(recipe), "locked": False}
            recipe_fragment = body
            total_items = 0
            pipeline_summary, pipeline_tone = "", "neutral"
        else:
            view = prebuilt_view if prebuilt_view is not None else self._current_view(recipe)
            recipe_fragment = body if body is not None else Markup(interface.render(view))
            total_items = sum(view.role_counts.values())
            pipeline_summary, pipeline_tone = self._pipeline_health(view)
        brand_dir = self._brand_dir(recipe, interface)
        brand_css_dir = self._brand_css_dir(brand_dir)
        context = {
            "title": interface.title,
            "subtitle": interface.subtitle.strip(),
            "view": view,
            "total_items": total_items,
            "notice": notice,
            "recipe_body": recipe_fragment,
            "pipeline_summary": pipeline_summary,
            "pipeline_tone": pipeline_tone,
            # Keep the steps panel open only when a Run inside it asked to stay open;
            # a failed step alone must not unsolicitedly open the table for reviewers.
            "pipeline_open": bool(notice and notice.open_pipeline),
            "brand_header": _fragment(brand_dir, "header.html"),
            "footer_html": self._resolve_footer_html(brand_dir, interface),
            "brand_css": brand_css_dir is not None,
            "review_mode": review_mode,
        }
        template_name = "interface_root.html.j2" if partial else "interface.html.j2"
        html = external_links_open_in_new_tab(
            self._shell_env.get_template(template_name).render(**context)
        )
        return self._html_response(html, brand_css_dir, "interface", extra_headers)

    def _poll_busy_status(self, recipe: Recipe) -> BusyStatus | None:
        """Lightweight busy check for `GET /_status`: avoids `build_view` while a run is active."""
        in_proc_busy, in_proc_label, in_proc_elapsed, in_proc_done, in_proc_total = (
            self._snapshot_in_process_activity()
        )
        ws_root = self.workspace_root.resolve()
        ext_locked, ext_since = (False, None) if in_proc_busy else _probe_external_lock(ws_root)
        locked = in_proc_busy or ext_locked
        if not locked:
            return None
        busy_source = "interface" if in_proc_busy else "external"
        db_path = ws_root / DB_NAME
        ledger_running: dict[str, Any] | None = None
        if db_path.is_file():
            try:
                conn = _open_current(db_path)
            except WorkspaceError:
                conn = None
            if conn is not None:
                try:
                    ledger_running = _inspect_running_ledger(conn, recipe.name)
                finally:
                    conn.close()
        return _make_busy_status(
            source=busy_source,
            in_process_label=in_proc_label,
            in_process_elapsed_seconds=in_proc_elapsed,
            external_since=ext_since,
            ledger_info=ledger_running,
            in_process_done=in_proc_done,
            in_process_total=in_proc_total,
        )

    def _render_busy_bar(self, title: str, busy: BusyStatus) -> HttpResponse:
        html = self._shell_env.get_template("interface_busy_bar.html.j2").render(
            title=title,
            view={"busy": busy},
        )
        return self._html_response(html, None, "interface")

    def _render_status_poll(self, recipe: Recipe) -> HttpResponse:
        """Serve the 5s HTMX busy poll (`/_status`); swap `#interface-root` when done."""
        interface = recipe.interface
        assert interface is not None
        busy = self._poll_busy_status(recipe)
        if busy is not None:
            return self._render_busy_bar(interface.title, busy)
        view = self._current_view(recipe)
        if view.locked and view.busy is not None:
            return self._render_busy_bar(interface.title, view.busy)

        fin_notice = self._pop_completed_notice()
        return self._render_html(
            recipe,
            partial=True,
            notice=fin_notice,
            prebuilt_view=view,
            extra_headers={
                "HX-Retarget": "#interface-root",
                "HX-Reswap": "outerHTML",
            },
        )

    def _text_error(self, status: int, message: str) -> HttpResponse:
        return _response(status, (message + "\n").encode("utf-8"), "text/plain; charset=utf-8")

    def _serve_file(self, base: Path, relative: str) -> HttpResponse:
        candidate = _resolve_under(base, relative)
        if candidate is None:
            return self._text_error(404, "not found")
        return _response(200, candidate.read_bytes(), _content_type(candidate), cache="no-cache")

    def _discover_output_snapshots(
        self,
        outputs_root: Path,
        stage_dir: str,
        rel_subpath: str,
        current_run_id: int,
    ) -> list[tuple[int, str, str]]:
        stage_root = outputs_root / stage_dir
        if not stage_root.is_dir():
            return []
        candidate_rids: list[int] = []
        for entry in stage_root.iterdir():
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            target = (entry / rel_subpath).resolve()
            if target.is_relative_to(outputs_root) and target.is_file():
                candidate_rids.append(int(entry.name))
        if not candidate_rids:
            return []
        candidate_rids.sort(reverse=True)

        run_meta: dict[int, tuple[str, str]] = {}
        db_path = self.workspace_root / DB_NAME
        if db_path.is_file():
            placeholders = ",".join("?" for _ in candidate_rids)
            conn = open_readonly(db_path)
            try:
                rows = conn.execute(
                    f"SELECT id, status, started_at, finished_at FROM runs"
                    f" WHERE id IN ({placeholders})",
                    candidate_rids,
                ).fetchall()
                for row in rows:
                    ts = _format_snapshot_timestamp(row["finished_at"] or row["started_at"])
                    run_meta[int(row["id"])] = (str(row["status"]), ts)
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        versions: list[tuple[int, str, str]] = []
        for rid in candidate_rids:
            meta = run_meta.get(rid)
            if meta is not None and rid != current_run_id and meta[0] in ("failed", "running"):
                continue
            url = "/outputs/" + urllib.parse.quote(f"{stage_dir}/{rid}/{rel_subpath}", safe="/")
            ts_str = meta[1] if meta is not None else ""
            versions.append((rid, url, ts_str))
        return versions

    def _serve_output(self, relative: str) -> HttpResponse:
        candidate = _resolve_under(self.workspace_root / OUTPUTS_DIR, relative)
        if candidate is None:
            return self._text_error(404, "not found")
        root = (self.workspace_root / OUTPUTS_DIR).resolve()
        raw_bytes = candidate.read_bytes()
        rel_parts = candidate.relative_to(root).parts
        html_text: str | None = None
        is_run_html = (
            candidate.suffix.lower() in (".html", ".htm")
            and len(rel_parts) >= 3
            and rel_parts[1].isdigit()
        )
        if is_run_html:
            try:
                html_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                html_text = None
        # Only `render_page` deliverables carry the shell slots; anything else is served as written.
        if html_text is None or "<!-- commissure-serve-controls -->" not in html_text:
            return _response(200, raw_bytes, _content_type(candidate), cache="no-cache")

        try:
            recipe = self._load_current_recipe()
        except RecipeError:
            recipe = self._cached_recipe
        interface = recipe.interface
        assert interface is not None

        stage_dir = rel_parts[0]
        current_run_id = int(rel_parts[1])
        rel_subpath = Path(*rel_parts[2:]).as_posix()
        versions = self._discover_output_snapshots(root, stage_dir, rel_subpath, current_run_id)
        latest_run_id = versions[0][0] if versions else current_run_id
        latest_url = versions[0][1] if versions else None
        is_historical = bool(versions and current_run_id != latest_run_id)

        ui_title = escape(interface.title)
        home_html = (
            '<nav class="app-back-nav" aria-label="Workspace navigation">'
            f'<a class="header-home-link" href="/">&larr; {ui_title}</a>'
            "</nav>"
        )

        pill_cls = "header-pill-btn header-pill-attention" if is_historical else "header-pill-btn"
        pill_label = (
            f"Snapshot #{current_run_id} (older)"
            if is_historical
            else f"Snapshot #{current_run_id}"
        )
        items_html: list[str] = []
        for rid, v_url, v_ts in versions:
            tag = " &middot; Latest" if rid == latest_run_id else ""
            ts_span = f'<span class="version-meta">{escape(v_ts)}</span>' if v_ts else ""
            if rid == current_run_id:
                view_tag = tag or " &middot; Viewing"
                items_html.append(
                    '<li class="version-item">'
                    '<span class="version-row is-current" aria-current="page">'
                    f"<span>Run #{rid}{view_tag}</span>{ts_span}</span></li>"
                )
            else:
                items_html.append(
                    '<li class="version-item">'
                    f'<a class="version-row" href="{escape(v_url)}">'
                    f"<span>Run #{rid}{tag}</span>{ts_span}</a></li>"
                )

        count_label = f"{len(versions)} snapshot{'s' if len(versions) != 1 else ''}"
        controls_html = (
            '<div class="app-header-controls">'
            '<details id="snapshot-versions-details" name="header-panel"'
            ' class="header-disclosure header-versions">'
            f'<summary class="{pill_cls}" aria-label="Snapshot version history">'
            f"{pill_label}</summary>"
            '<div class="header-popover-card header-popover-right header-versions-card">'
            '<div class="status-card-title"><strong>Version History</strong>'
            f'<span class="version-meta">{count_label}</span></div>'
            f'<ul class="version-list">{"".join(items_html)}</ul>'
            "</div></details></div>"
        )

        banner_html = ""
        if is_historical and latest_url is not None:
            cur_ts = next((ts for rid, _, ts in versions if rid == current_run_id), "")
            ts_note = f" ({escape(cur_ts)})" if cur_ts else ""
            banner_html = (
                '<div class="notice notice-busy snapshot-banner" role="status">'
                "<span><strong>Viewing historical snapshot</strong>"
                f" &mdash; Run #{current_run_id}{ts_note}.</span>"
                f'<a href="{escape(latest_url)}">'
                f"Switch to latest (Run #{latest_run_id}) &rarr;</a>"
                "</div>"
            )

        served = html_text
        if "data-commissure-served=" not in served:
            served = re.sub(
                r"<html\b",
                '<html data-commissure-served="true"',
                served,
                count=1,
                flags=re.IGNORECASE,
            )
        served = served.replace("<!-- commissure-serve-home -->", home_html, 1)
        served = served.replace("<!-- commissure-serve-controls -->", controls_html, 1)
        served = served.replace("<!-- commissure-serve-banner -->", banner_html, 1)
        served = external_links_open_in_new_tab(served)
        brand_css_dir = self._brand_css_dir(self._brand_dir(recipe, interface))
        return self._html_response(served, brand_css_dir, "deliverable")

    def _parse_form(self, headers: Mapping[str, str], body: bytes) -> dict[str, str]:
        ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype == "application/json":
            raw = json.loads(body.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected JSON object")
            return {str(k): "" if v is None else str(v) for k, v in raw.items()}
        parsed = urllib.parse.parse_qs(
            body.decode("utf-8", errors="replace"), keep_blank_values=True
        )
        return {k: vals[-1] if vals else "" for k, vals in parsed.items()}

    def handle(
        self,
        method: str,
        raw_target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> HttpResponse:
        norm_headers = {k.lower(): v for k, v in headers.items()}
        split = urllib.parse.urlsplit(raw_target)
        path = split.path or "/"
        query = urllib.parse.parse_qs(split.query)

        if method == "GET":
            if path == "/":
                try:
                    recipe = self._load_current_recipe()
                    notice_id = query.get("notice", [None])[-1]
                    view = self._current_view(recipe)
                    notice = self._pop_notice(notice_id)
                    if notice is None and not view.locked:
                        notice = self._pop_completed_notice()
                    return self._render_html(
                        recipe, partial=False, notice=notice, prebuilt_view=view
                    )
                except (RecipeError, WorkspaceError) as exc:
                    return self._text_error(500, str(exc))

            if path == "/_status":
                try:
                    recipe = self._load_current_recipe()
                    return self._render_status_poll(recipe)
                except (RecipeError, WorkspaceError) as exc:
                    return self._text_error(500, str(exc))

            if path.startswith("/static/brand/"):
                try:
                    recipe = self._load_current_recipe()
                except RecipeError as exc:
                    return self._text_error(500, str(exc))
                assert recipe.interface is not None
                bdir = self._brand_dir(recipe, recipe.interface)
                rel = path.removeprefix("/static/brand/")
                if bdir is None or rel in _INLINE_BRAND:
                    return self._text_error(404, "not found")
                return self._serve_file(bdir, rel)

            if path.startswith("/static/"):
                return self._serve_file(STATIC_DIR, path.removeprefix("/static/"))

            if path.startswith("/outputs/"):
                return self._serve_output(path.removeprefix("/outputs/"))

            if path == "/review" or path.startswith("/review/"):
                try:
                    return self._review.get(path, query, norm_headers)
                except (RecipeError, WorkspaceError) as exc:
                    return self._text_error(500, str(exc))

            return self._text_error(404, "not found")

        if method != "POST":
            return self._text_error(405, "method not allowed")

        if len(body) > _MAX_FORM_BYTES:
            return self._text_error(413, "request body too large")

        try:
            form = self._parse_form(norm_headers, body)
        except ValueError as exc:
            return self._text_error(400, f"malformed request body: {exc}")

        provided_csrf = form.get("csrf_token") or norm_headers.get("x-csrf-token") or ""
        if not provided_csrf or not secrets.compare_digest(provided_csrf, self.csrf_token):
            return self._text_error(403, "invalid or missing CSRF token")

        # Verdict writes never take the run mutex: review goes on while a run is in progress.
        if path.startswith("/review/"):
            try:
                handled = self._review.post(path, form, norm_headers)
            except (RecipeError, WorkspaceError) as exc:
                return self._text_error(500, str(exc))
            if handled is not None:
                return handled

        try:
            recipe = self._load_current_recipe()
        except RecipeError as exc:
            return self._text_error(500, str(exc))
        assert recipe.interface is not None

        is_pipeline_route = path == "/_pipeline/run"
        is_action_route = path.startswith("/actions/")
        if not is_pipeline_route and not is_action_route:
            return self._text_error(404, "not found")

        action_name = path.removeprefix("/actions/") if is_action_route else ""
        if is_action_route and action_name not in recipe.interface.actions:
            return self._text_error(404, f"unknown action {action_name!r}")

        is_htmx = norm_headers.get("hx-request", "").lower() == "true"

        if not self._run_mutex.acquire(blocking=False):
            notice = Notice(
                "Another run is already in progress in this interface; wait for it to finish.",
                is_error=True,
                open_pipeline=is_pipeline_route,
            )
            return self._finish_post(recipe, notice, is_htmx=is_htmx)

        if is_pipeline_route:
            raw_step = form.get("step", "").strip()
            initial_label = (
                "Running: pipeline"
                if raw_step == "__all__"
                else f"Running: {raw_step or 'pipeline'}"
            )
        else:
            initial_label = f"Running: {action_name.replace('_', ' ')}"

        done_event = threading.Event()
        cancel_event = threading.Event()
        with self._state_lock:
            self._completed_notice = None
            self._active_label = initial_label
            self._active_done = None
            self._active_total = None
            self._active_started_mono = time.monotonic()
            self._active_done_event = done_event
            self._active_cancel_event = cancel_event

        def _worker() -> None:
            worker_notice: Notice
            try:
                assert recipe.interface is not None
                ctx = ActionContext(
                    self.workspace_root,
                    recipe,
                    status_callback=self._set_active_label,
                    cancel_event=cancel_event,
                )
                try:
                    if is_pipeline_route:
                        step = form.get("step", "").strip()
                        if not step:
                            raise ActionError("step parameter is required")
                        if step == "__all__":
                            runs = ctx.run_pipeline()
                            ran_labels = ", ".join(f"{r.step} ({r.status})" for r in runs)
                            msg = f"Completed pipeline in order: {ran_labels}."
                        else:
                            res = ctx.run_step(step)
                            counts = _count_summary(
                                res.valid, res.warning, res.quarantined, res.skipped
                            )
                            msg = f"Step {res.step} {res.status} ({counts})."
                        worker_notice = Notice(msg, is_error=False, open_pipeline=True)
                    else:
                        handler = recipe.interface.actions[action_name]
                        msg = handler(ctx, MappingProxyType(form))
                        worker_notice = Notice(str(msg), is_error=False, open_pipeline=False)
                except ActionError as exc:
                    worker_notice = Notice(str(exc), is_error=True, open_pipeline=is_pipeline_route)
            except Exception as exc:  # pragma: no cover - defensive guard for worker thread
                worker_notice = Notice(
                    f"Unexpected error: {type(exc).__name__}: {exc}",
                    is_error=True,
                    open_pipeline=is_pipeline_route,
                )
            finally:
                with self._state_lock:
                    self._completed_notice = worker_notice
                    self._active_label = None
                    self._active_done = None
                    self._active_total = None
                    self._active_started_mono = None
                    self._active_done_event = None
                    self._active_cancel_event = None
                    self._run_mutex.release()
                done_event.set()

        worker_thread = threading.Thread(
            target=_worker,
            name="commissure-interface-worker",
            daemon=True,
        )
        worker_thread.start()

        if not is_htmx:
            done_event.wait()
            fin_notice = self._pop_completed_notice() or Notice("Run finished.")
            return self._finish_post(recipe, fin_notice, is_htmx=False)

        if done_event.wait(timeout=self._htmx_wait_seconds):
            fin_notice = self._pop_completed_notice()
            return self._render_html(recipe, partial=True, notice=fin_notice)

        return self._render_html(recipe, partial=True, notice=None)

    def _finish_post(self, recipe: Recipe, notice: Notice, *, is_htmx: bool) -> HttpResponse:
        if is_htmx:
            return self._render_html(recipe, partial=True, notice=notice)
        token = self._store_notice(notice)
        return HttpResponse(
            status=303,
            headers={
                "Location": f"/?notice={urllib.parse.quote(token)}",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            body=b"",
        )


def make_server(
    app: InterfaceApp, host: str = "127.0.0.1", port: int = 8000
) -> ThreadingHTTPServer:
    """Create a stdlib `ThreadingHTTPServer` bound to `(host, port)` serving `app`."""

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length_raw = self.headers.get("Content-Length")
            body = b""
            if length_raw:
                try:
                    length = int(length_raw)
                except ValueError:
                    self._write(app._text_error(400, "invalid Content-Length"))
                    return
                if length < 0:
                    self._write(app._text_error(400, "invalid Content-Length"))
                    return
                if length > _MAX_FORM_BYTES:
                    self._write(app._text_error(413, "request body too large"))
                    return
                body = self.rfile.read(length)
            resp = app.handle(method, self.path, dict(self.headers.items()), body)
            self._write(resp)

        def _write(self, resp: HttpResponse) -> None:
            try:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                if resp.file is not None:
                    with resp.file.open("rb") as src:
                        self.send_header("Content-Length", str(os.fstat(src.fileno()).st_size))
                        self.end_headers()
                        shutil.copyfileobj(src, self.wfile, 1 << 20)
                    return
                self.send_header("Content-Length", str(len(resp.body)))
                self.end_headers()
                if resp.body:
                    self.wfile.write(resp.body)
            except (BrokenPipeError, ConnectionResetError):
                # Browser closed the socket early (reload, tab close, or superseded HTMX poll).
                return

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # keep terminal clean; runs are recorded in state.db

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64

    return _Server((host, port), _Handler)
