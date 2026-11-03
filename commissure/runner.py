"""Bounded, checkpointed execution of one stage over its subjects.

Every stage (core parse, recipe stages) runs through `run_plan`:

1. The plan's subjects are built inside the run, so a planning failure is recorded.
2. Done subjects are set aside. In cache mode (parse) a subject is done when an
   outcome with the same stage, subject, version, input and stage fingerprint exists
   in any run. Otherwise (recipe stages: snapshots) a subject is done only if the
   run being resumed already recorded it with the same input fingerprint. Recipe
   fingerprints are labels and a resume guard, never a cache key: a fingerprint that
   missed a dependency (a data file, a library) would silently reuse old output under
   new code, where a snapshot only mislabels staleness.
3. Pending subjects run on a bounded sliding window over `workers` threads (with core
   `parse` delegating deterministic extraction to a bounded pool of local child processes
   so wall-clock timeouts and interrupts preempt GIL/C work). The workspace holds one
   shared SQLite connection with all access serialized by `_CONN_LOCK`. Worker threads
   may touch SQLite only through `ctx.llm` (stage functions must not use the workspace
   connection from a worker; `ctx.search` raises if called from a worker). The main
   thread commits each subject on its own (its result row, its outcome, its lineage),
   so an interrupt loses at most the subjects in flight and a retry never counts one
   twice.
4. Fail-closed defaults (Records 5): empty discovery fails unless permitted; a run
   whose attempted work was all quarantined fails; nothing pending is a noop. The
   recipe's gate judges the snapshot once it is complete.

A run resumes the previous run of the same step when that run did not finish its
scope (interrupted, faulted, abandoned, or cut short by --limit) and the stage
fingerprint, recipe fingerprint, and parameters are unchanged. `fresh` refuses.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from commissure.limits import Limits, is_limit_quarantine_stale
from commissure.llm import (
    LlmSeam,
    SystematicModelFailure,
    Transport,
    validate_stage_llm,
)
from commissure.progress import ProgressSnapshot
from commissure.records import RecordEnvelope
from commissure.runs import EmptyDiscovery, finish_run, start_run
from commissure.workspace import Workspace, transaction, utc_now

ProgressCallback = Callable[[ProgressSnapshot], None]

_local = threading.local()


def _current_subject_key() -> str:
    return getattr(_local, "subject_key", "run")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AllQuarantined(Exception):
    """Every subject the run attempted was quarantined."""


class NothingAttempted(Exception):
    """Subjects were discovered, pending work existed, and nothing was attempted."""


class GateFailed(Exception):
    """The recipe's acceptance gate rejected the completed snapshot."""


class RunCancelled(Exception):
    """The run was cancelled cooperatively by an interrupt or server shutdown."""


class _RunCancelEvent:
    """Per-run cancellation view: trips if the caller cancels or this single run aborts.

    Isolating `_local` prevents a stage-level failure (such as tolerated `AllQuarantined`
    on `parse:ocr`) from poisoning a multi-step `ActionContext`'s shared `cancel_event`.
    """

    def __init__(self, parent: threading.Event | None = None) -> None:
        self._parent = parent
        self._local = threading.Event()

    def is_set(self) -> bool:
        return self._local.is_set() or (self._parent is not None and self._parent.is_set())

    def set(self) -> None:
        self._local.set()

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        if self._parent is None:
            return self._local.wait(timeout=timeout)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            if deadline is not None:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return self.is_set()
                self._local.wait(timeout=min(0.02, rem))
            else:
                self._local.wait(timeout=0.02)
        return True


class RunContext:
    """What a stage function can use while it runs."""

    def __init__(
        self,
        run_id: int,
        stage: str,
        params: dict[str, Any],
        workers: int,
        outputs_root: Path | None = None,
        llm: LlmSeam | None = None,
        conn: sqlite3.Connection | None = None,
        cancel_event: threading.Event | _RunCancelEvent | None = None,
    ) -> None:
        self.run_id = run_id
        self.stage = stage
        self.params = MappingProxyType(dict(params))
        self.workers = workers
        self._outputs_root = outputs_root
        self.llm = llm
        self._conn = conn
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        # Set by recipe_plan.execute when Stage.reads_review; None otherwise.
        self._review_report_json: str | None = None

    @property
    def outputs(self) -> Path:
        """Per-run output directory `<workspace>/outputs/<stage>/<run_id>/`, created on access."""
        if self._outputs_root is None:
            raise RuntimeError("RunContext has no workspace outputs directory")
        path = self._outputs_root / self.stage / str(self.run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def review_report(self) -> dict[str, Any]:
        """The plan-time review snapshot. Requires `Stage(reads_review=True)`.

        Returns a fresh parse of the frozen JSON; does not query SQLite. Safe from a
        worker thread. A recipe that reads verdicts without the flag would publish a
        rate the input fingerprint does not cover, so this raises instead.
        """
        if self._review_report_json is None:
            raise RuntimeError(
                "ctx.review_report requires Stage(reads_review=True);"
                " the stage's input fingerprint must include the review snapshot"
            )
        return json.loads(self._review_report_json)

    def search(
        self,
        query: str,
        *,
        parser: str | None = None,
        limit: int = 20,
        phrase: bool = True,
    ):
        """Keyword/phrase search over current parsed text. Main thread only."""
        from commissure.search import SearchFromWorker, search

        if getattr(_local, "in_worker", False):
            raise SearchFromWorker(
                "ctx.search cannot run from a worker; call it from the main thread"
            )
        if self._conn is None:
            raise RuntimeError("RunContext has no workspace connection")
        return search(self._conn, query, parser=parser, limit=limit, phrase=phrase)

    def map(self, fn: Callable[[Any], Any], items: Iterable[Any]) -> list[Any]:
        """`fn` over `items` on at most `workers` threads; results in input order.

        Inside a worker thread this runs sequentially, so nesting never multiplies the
        thread count. Stage functions must not use the workspace connection from a
        worker; worker threads may touch SQLite only through `ctx.llm` under `_CONN_LOCK`.
        """
        items = list(items)
        if self.workers <= 1 or len(items) <= 1 or getattr(_local, "in_worker", False):
            return [fn(x) for x in items]
        parent_subject = getattr(_local, "subject_key", "run")
        with ThreadPoolExecutor(self.workers) as pool:
            return list(pool.map(_as_worker(fn, subject_key=parent_subject), items))


def _as_worker(fn: Callable[..., Any], *, subject_key: str | None = None) -> Callable[..., Any]:
    def call(*args: Any) -> Any:
        _local.in_worker = True
        prev_subject = getattr(_local, "subject_key", None)
        if subject_key is not None:
            _local.subject_key = subject_key
        elif len(args) >= 2 and isinstance(args[1], Subject):
            _local.subject_key = args[1].key
        try:
            return fn(*args)
        finally:
            _local.in_worker = False
            if prev_subject is None:
                if hasattr(_local, "subject_key"):
                    delattr(_local, "subject_key")
            else:
                _local.subject_key = prev_subject

    return call


@dataclass
class Subject:
    stage: str
    stage_fp: str
    kind: str  # item | group | run
    key: str
    item_version_id: int | None
    input: Any
    input_fp: str
    consumed: tuple[int, ...] = ()
    skip: RecordEnvelope | None = None  # decided while planning; never executed


@dataclass
class Plan:
    step: str
    stage_fp: str
    params: dict[str, Any]
    subjects: Callable[[], list[Subject]]
    execute: Callable[[RunContext, Subject], RecordEnvelope]
    store: Callable[[sqlite3.Connection, int, Subject, RecordEnvelope], dict[str, int]]
    recipe_fp: str | None = None
    cache: bool = False
    allow_empty: bool = False
    allow_nothing_attempted: bool = False
    fail_if_all_quarantined: bool = True
    gate: Callable[[dict[str, int]], list[str]] | None = None
    limits: Limits | None = None
    close: Callable[[], None] | None = None


@dataclass
class RunResult:
    run_id: int
    status: str
    discovered: int = 0
    unchanged: int = 0
    attempted: int = 0
    valid: int = 0
    warning: int = 0
    quarantined: int = 0
    skipped: int = 0
    remaining: int = 0
    resumed_run_id: int | None = None
    not_resumed: str | None = None
    gate: str | None = None  # passed | failed | deferred (snapshot incomplete) | None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(d.pop("extra"))
        return d

    def counts(self) -> dict[str, int]:
        keys = (
            "discovered",
            "unchanged",
            "attempted",
            "valid",
            "warning",
            "quarantined",
            "skipped",
        )
        return {k: getattr(self, k) for k in keys} | {"remaining": self.remaining}


def reap_abandoned(conn: sqlite3.Connection) -> int:
    """Mark runs left 'running' by a process that died. Call while holding the lock."""
    with transaction(conn):
        return conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ?, error = ?"
            " WHERE status = 'running'",
            (utc_now(), "abandoned: the process ended before the run finished"),
        ).rowcount


def _resume_target(
    conn: sqlite3.Connection, plan: Plan, fresh: bool
) -> tuple[int | None, str | None]:
    prev = conn.execute(
        "SELECT * FROM runs WHERE step = ? ORDER BY id DESC LIMIT 1", (plan.step,)
    ).fetchone()
    if prev is None:
        return None, None
    unfinished = prev["remaining"] is None or prev["remaining"] > 0
    if not unfinished:
        return None, None
    if fresh:
        return None, f"--fresh: run {prev['id']} not resumed"
    for column, now in (
        ("recipe_fp", plan.recipe_fp),
        ("stage_fp", plan.stage_fp),
        ("params", canonical(plan.params)),
    ):
        if prev[column] != now:
            what = column.replace("_fp", "").replace("params", "parameters")
            return None, f"run {prev['id']} not resumed: {what} changed since it ran"
    return prev["id"], None


def _chain(conn: sqlite3.Connection, run_id: int | None) -> list[int]:
    chain = []
    while run_id is not None:
        chain.append(run_id)
        run_id = conn.execute("SELECT resumes_run_id FROM runs WHERE id = ?", (run_id,)).fetchone()[
            0
        ]
    return chain


def _done(conn: sqlite3.Connection, plan: Plan, subjects: list[Subject], chain: list[int]):
    if plan.cache:
        stages = sorted({s.stage for s in subjects})
        marks = ",".join("?" * len(stages))
        rows = conn.execute(
            f"SELECT stage, subject_key, item_version_id, input_fp, stage_fp,"
            f" outcome, reason_code, diagnostics FROM outcomes"
            f" WHERE stage IN ({marks}) AND params = ? ORDER BY id ASC",
            (*stages, canonical(plan.params)),
        )
        latest: dict[tuple[Any, ...], tuple[str, str | None, str | None]] = {}
        for r in rows:
            key = (r["stage"], r["subject_key"], r["item_version_id"], r["input_fp"], r["stage_fp"])
            latest[key] = (r["outcome"], r["reason_code"], r["diagnostics"])

        def _cached_done(s: Subject) -> bool:
            hit = latest.get((s.stage, s.key, s.item_version_id, s.input_fp, s.stage_fp))
            if hit is None:
                return False
            outcome, reason_code, diagnostics = hit
            return not is_limit_quarantine_stale(
                s.stage, outcome, reason_code, diagnostics, plan.limits
            )

        return _cached_done
    if not chain:
        return lambda s: False
    marks = ",".join("?" * len(chain))
    done = {
        tuple(r)
        for r in conn.execute(
            f"SELECT stage, subject_key, input_fp FROM outcomes WHERE run_id IN ({marks})", chain
        )
    }
    return lambda s: (s.stage, s.key, s.input_fp) in done


def run_plan(
    ws: Workspace,
    plan: Plan,
    *,
    trigger: str,
    config: dict[str, Any],
    limit: int | None = None,
    workers: int = 1,
    fresh: bool = False,
    no_cache: bool = False,
    transport: Transport | None = None,
    cancel_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
) -> RunResult:
    validate_stage_llm(plan.params)
    cancel = _RunCancelEvent(cancel_event)
    conn = ws.conn
    resume_of, not_resumed = (None, None) if plan.cache else _resume_target(conn, plan, fresh)
    run_id = start_run(
        conn,
        plan.step,
        trigger=trigger,
        config=config,
        stage_fp=plan.stage_fp,
        recipe_fp=plan.recipe_fp,
        params=canonical(plan.params),
        resumes_run_id=resume_of,
        override="fresh" if fresh else None,
    )
    result = RunResult(
        run_id=run_id, status="running", resumed_run_id=resume_of, not_resumed=not_resumed
    )
    try:
        _run(
            ws,
            plan,
            result,
            limit,
            workers,
            _chain(conn, resume_of),
            no_cache=no_cache,
            transport=transport,
            cancel=cancel,
            on_progress=on_progress,
        )
    except BaseException as exc:  # Ctrl-C still leaves an honest ledger row
        cancel.set()
        if plan.close is not None:
            plan.close()
        result.status = "failed"
        err_msg = f"{type(exc).__name__}: {exc}"
        subj_tag = getattr(exc, "_failed_subject", None)
        if subj_tag and "subject=" not in err_msg:
            err_msg = f"{err_msg} [subject={subj_tag}]"
        result.error = err_msg
        with transaction(conn):
            finish_run(conn, run_id, "failed", result.error, **result.counts())
        if not isinstance(exc, Exception):
            raise
    finally:
        if plan.close is not None:
            plan.close()
    return result


def _run(
    ws: Workspace,
    plan: Plan,
    result: RunResult,
    limit: int | None,
    workers: int,
    chain: list[int],
    *,
    no_cache: bool = False,
    transport: Transport | None = None,
    cancel: threading.Event | _RunCancelEvent,
    on_progress: ProgressCallback | None = None,
) -> None:
    conn = ws.conn
    subjects = plan.subjects()
    result.discovered = len(subjects)
    if not subjects and not plan.allow_empty:
        raise EmptyDiscovery("no subjects in scope; pass --allow-empty to permit")

    is_done = _done(conn, plan, subjects, chain)
    pending = [s for s in subjects if not is_done(s)]
    result.unchanged = len(subjects) - len(pending)
    todo = pending if limit is None else pending[:limit]
    result.remaining = len(pending)  # counts down as subjects commit
    # Write discovery counts while status='running' so concurrent WAL readers (CLI inspect and
    # `commissure serve`) can report determinate `done / total` progress; `remaining` stays
    # `len(pending)` so a killed process is still recognized as unfinished by `_resume_target`.
    with transaction(conn):
        conn.execute(
            "UPDATE runs SET discovered = ?, unchanged = ?, remaining = ? WHERE id = ?",
            (result.discovered, result.unchanged, result.remaining, result.run_id),
        )

    progress_total = len(todo)
    pre_skipped = sum(1 for s in todo if s.skip is not None)
    started = time.monotonic()
    current_subject: list[str | None] = [None]

    def _emit(current: str | None = None) -> None:
        if on_progress is None:
            return
        if current is not None:
            current_subject[0] = current
        done_count = result.valid + result.warning + result.quarantined + result.skipped
        on_progress(
            ProgressSnapshot(
                label=plan.step,
                done=done_count,
                total=progress_total,
                elapsed_seconds=time.monotonic() - started,
                warning=result.warning,
                quarantined=result.quarantined,
                current=current_subject[0],
                skipped=min(pre_skipped, done_count),
            )
        )

    _emit()

    llm: LlmSeam | None = None
    if plan.params.get("model") is not None:
        llm = LlmSeam(
            conn=conn,
            run_id=result.run_id,
            stage=plan.step,
            stage_params=plan.params,
            no_cache=no_cache,
            transport=transport,
            subject_key_getter=_current_subject_key,
            # A heartbeat before each model call; `current` stays as the loop set it, since
            # from worker threads the calling subject would flicker between subjects.
            on_before_call=(lambda _key: _emit()) if on_progress is not None else None,
        )
    ctx = RunContext(
        result.run_id,
        plan.step,
        plan.params,
        workers,
        outputs_root=ws.outputs,
        llm=llm,
        conn=conn,
        cancel_event=cancel,
    )
    for s in [s for s in todo if s.skip is not None]:
        _write(conn, plan, result, s, s.skip)
        _emit()
    runnable = [s for s in todo if s.skip is None]
    if workers <= 1 or len(runnable) <= 1:
        for s in runnable:
            if cancel.is_set():
                raise RunCancelled("interrupted by shutdown")
            _local.subject_key = s.key
            _emit(current=s.key)
            try:
                env = plan.execute(ctx, s)
            except BaseException as exc:
                if not isinstance(exc, (RunCancelled, SystematicModelFailure)):
                    exc._failed_subject = _subject_label(s)  # type: ignore[attr-defined]
                raise
            finally:
                if hasattr(_local, "subject_key"):
                    delattr(_local, "subject_key")
            if cancel.is_set():
                raise RunCancelled("interrupted by shutdown")
            try:
                _write(conn, plan, result, s, env)
                _emit()
            except BaseException as exc:
                if not isinstance(exc, (RunCancelled, SystematicModelFailure)):
                    exc._failed_subject = _subject_label(s)  # type: ignore[attr-defined]
                raise
            if llm is not None and llm.breaker_error is not None:
                raise llm.breaker_error
    else:
        # Submit a bounded sliding window (`workers * 2`) rather than all pending subjects
        # at once so 250k-item runs keep memory O(workers) and interrupts stop immediately.
        pool = ThreadPoolExecutor(workers)
        in_flight: dict[Future[RecordEnvelope], Subject] = {}

        def _oldest_in_flight() -> str | None:
            # Dicts keep submission order, so the first entry has run longest: the one
            # worth naming when a run looks stuck, not the one submitted last.
            return next(iter(in_flight.values())).key if in_flight else None

        it = iter(runnable)
        window = workers * 2
        try:
            while len(in_flight) < window:
                if cancel.is_set():
                    raise RunCancelled("interrupted by shutdown")
                nxt = next(it, None)
                if nxt is None:
                    break
                in_flight[pool.submit(_as_worker(plan.execute), ctx, nxt)] = nxt
            _emit(current=_oldest_in_flight())

            while in_flight:
                if cancel.is_set():
                    raise RunCancelled("interrupted by shutdown")
                done_set, _ = wait(in_flight, timeout=0.05, return_when=FIRST_COMPLETED)
                if not done_set:
                    continue
                for future in done_set:
                    subj = in_flight.pop(future)
                    try:
                        env = future.result()
                        if cancel.is_set():
                            raise RunCancelled("interrupted by shutdown")
                        _write(conn, plan, result, subj, env)
                        _emit(current=_oldest_in_flight())
                    except BaseException as exc:
                        if not isinstance(exc, (RunCancelled, SystematicModelFailure)):
                            exc._failed_subject = _subject_label(subj)  # type: ignore[attr-defined]
                        raise
                    if llm is not None and llm.breaker_error is not None:
                        for pending_f in list(in_flight):
                            pending_f.cancel()
                        for pending_f, rem_subj in list(in_flight.items()):
                            in_flight.pop(pending_f, None)
                            if pending_f.cancelled():
                                continue
                            _write(conn, plan, result, rem_subj, pending_f.result())
                            _emit()
                        raise llm.breaker_error
                    if not cancel.is_set():
                        nxt = next(it, None)
                        if nxt is not None:
                            in_flight[pool.submit(_as_worker(plan.execute), ctx, nxt)] = nxt
                            _emit(current=_oldest_in_flight())
        except BaseException:
            cancel.set()
            for pending_f in list(in_flight):
                pending_f.cancel()
            if plan.close is not None:
                plan.close()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    if cancel.is_set():
        raise RunCancelled("interrupted by shutdown")
    snapshot = _snapshot_counts(conn, result, chain)
    snapshot_attempted = snapshot["valid"] + snapshot["warning"] + snapshot["quarantined"]
    if not pending:
        result.status = "noop"
    elif result.discovered > 0 and snapshot_attempted == 0 and not plan.allow_nothing_attempted:
        reasons = _skip_summary(conn, [result.run_id, *chain])
        raise NothingAttempted(
            f"nothing_attempted: discovered {result.discovered}, attempted 0"
            + (f" (skipped: {reasons})" if reasons else "")
        )
    elif (
        plan.fail_if_all_quarantined and result.attempted and result.quarantined == result.attempted
    ):
        reasons = _quarantine_summary(conn, [result.run_id])
        raise AllQuarantined(
            f"all {result.attempted} attempted subjects were quarantined"
            + (f" ({reasons})" if reasons else "")
        )
    else:
        result.status = "succeeded"
    if plan.gate is not None and result.status == "succeeded":
        if result.remaining:
            result.gate = "deferred"
        else:
            failures = plan.gate(snapshot)
            result.gate = "failed" if failures else "passed"
            if failures:
                raise GateFailed("; ".join(failures))
    with transaction(conn):
        finish_run(conn, result.run_id, result.status, **result.counts())


def _subject_label(s: Subject) -> str:
    loc = getattr(s.input, "locator", None)
    if isinstance(loc, str) and loc and loc != s.key:
        return f"{s.key} ({loc})"
    return s.key


def _skip_summary(conn: sqlite3.Connection, run_ids: list[int]) -> str:
    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT coalesce(reason_code, 'unknown'), count(*) FROM outcomes"
        f" WHERE run_id IN ({marks}) AND outcome = 'skipped'"
        f" GROUP BY reason_code ORDER BY reason_code",
        run_ids,
    ).fetchall()
    return ", ".join(f"{code}={n}" for code, n in rows)


def _quarantine_summary(conn: sqlite3.Connection, run_ids: list[int]) -> str:
    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT coalesce(reason_code, 'unknown'), count(*) FROM outcomes"
        f" WHERE run_id IN ({marks}) AND outcome = 'quarantined'"
        f" GROUP BY reason_code ORDER BY reason_code",
        run_ids,
    ).fetchall()
    summary = ", ".join(f"{code}={n}" for code, n in rows)
    total = sum(n for _, n in rows)
    if total == 1:
        diag_row = conn.execute(
            f"SELECT subject_key, diagnostics FROM outcomes"
            f" WHERE run_id IN ({marks}) AND outcome = 'quarantined' LIMIT 1",
            run_ids,
        ).fetchone()
        if diag_row is not None:
            try:
                diags = json.loads(diag_row[1] or "[]")
            except ValueError:
                diags = []
            first_msg = diags[0].get("message") if diags and isinstance(diags[0], dict) else None
            if first_msg:
                return f"{summary}; {diag_row[0]}: {first_msg}"
    return summary


def _snapshot_counts(conn: sqlite3.Connection, result: RunResult, chain: list[int]) -> dict:
    """Outcome counts over the whole snapshot: this run plus the runs it resumed."""
    counts = {k: getattr(result, k) for k in ("valid", "warning", "quarantined", "skipped")}
    if chain:
        marks = ",".join("?" * len(chain))
        for outcome, n in conn.execute(
            f"SELECT outcome, count(*) FROM outcomes WHERE run_id IN ({marks}) GROUP BY outcome",
            chain,
        ):
            counts[outcome] += n
    return {"discovered": result.discovered, **counts}


def _write(conn, plan: Plan, result: RunResult, s: Subject, env: RecordEnvelope) -> None:
    with transaction(conn):
        ids = plan.store(conn, result.run_id, s, env) if env.payload is not None else {}
        outcome_id = conn.execute(
            "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key, item_version_id,"
            " input_fp, stage_fp, params, outcome, reason_code, diagnostics,"
            " extracted_text_id, record_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.run_id,
                s.stage,
                s.kind,
                s.key,
                s.item_version_id,
                s.input_fp,
                s.stage_fp,
                canonical(plan.params),
                env.outcome,
                env.reason_code,
                canonical([d.model_dump(exclude_defaults=True) for d in env.diagnostics]),
                ids.get("extracted_text_id"),
                ids.get("record_id"),
                utc_now(),
            ),
        ).lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO outcome_inputs (outcome_id, upstream_outcome_id) VALUES (?, ?)",
            [(outcome_id, u) for u in s.consumed],
        )
    setattr(result, env.outcome, getattr(result, env.outcome) + 1)
    if env.outcome != "skipped":
        result.attempted += 1
    result.remaining -= 1
