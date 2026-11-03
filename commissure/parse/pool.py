"""Bounded local child-process pool for deterministic `parse` execution.

Why child processes for `parse` (and threads for everything else):
1. Python's PDF, OOXML, XML, and OCR libraries execute inside GIL-holding C extensions or
   child binaries (`pypdfium2`, `lxml`, `openpyxl`, `tesseract`). A Python thread cannot
   be preempted mid-call when a pathological document stalls or when the operator presses
   `Ctrl+C`.
2. Running parse workers in spawned local child processes lets the parent enforce hard
   per-item wall-clock timeouts (`max_parse_seconds` / `max_ocr_seconds`), terminate
   workers and any grandchild `tesseract` processes immediately on interrupt or server
   shutdown, isolate C-extension crashes to a single `quarantined` item, and recycle
   workers periodically so the OS reclaims native memory arenas.
3. Workers never touch SQLite (`state.db`); they read a blob path and return a
   `RecordEnvelope` over a local pipe, while the parent thread commits outcomes as usual.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path

from commissure.limits import DEFAULT_LIMITS, Limits
from commissure.records import Diagnostic, RecordEnvelope
from commissure.runner import RunCancelled

# Recycle worker processes periodically (and immediately after large payloads) so native
# PDFium / lxml / pymalloc arenas return to the OS during multi-thousand-file runs.
MAX_TASKS_PER_WORKER = 64
RECYCLE_PAYLOAD_BYTES = 8 * 1024 * 1024
_POLL_SLICE_SECONDS = 0.05


@dataclass(eq=False)
class _WorkerSlot:
    proc: multiprocessing.Process
    conn: Connection
    tasks_completed: int = 0

    def __post_init__(self) -> None:
        self._term_lock = threading.Lock()
        self._terminated = False


def _worker_main(conn: Connection) -> None:
    """Child entrypoint: isolate process group, ignore terminal SIGINT, serve parse requests."""
    if hasattr(os, "setpgid"):
        with contextlib.suppress(OSError):
            os.setpgid(0, 0)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    from commissure.parse.step import execute_parse_local

    try:
        while True:
            try:
                msg = conn.recv()
            except EOFError:
                break
            if msg is None:
                break
            parser_name, blob_str, limits, test_sleep_s = msg
            env = execute_parse_local(
                parser_name,
                Path(blob_str),
                limits,
                _test_sleep_s=test_sleep_s,
            )
            conn.send(env)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError):
            conn.close()


def _terminate_slot(slot: _WorkerSlot, *, graceful: bool = False) -> None:
    """Close pipe and reap worker process group (including any grandchild `tesseract`)."""
    with slot._term_lock:
        if slot._terminated:
            return
        slot._terminated = True
        with contextlib.suppress(OSError):
            slot.conn.close()

        with contextlib.suppress(OSError, ValueError):
            pid = slot.proc.pid
            if pid is not None and slot.proc.is_alive():
                if graceful:
                    slot.proc.join(timeout=0.15)
                if slot.proc.is_alive():
                    if hasattr(os, "killpg"):
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            with contextlib.suppress(OSError):
                                slot.proc.kill()
                    else:  # pragma: no cover - Windows fallback
                        with contextlib.suppress(OSError):
                            slot.proc.kill()
            if pid is not None:
                slot.proc.join(timeout=1.0)
                if not slot.proc.is_alive():
                    slot.proc.close()


class ParseProcessPool:
    """Lazily spawned, bounded pool of local `spawn` processes for `parse` items."""

    def __init__(self, max_workers: int = 1, limits: Limits = DEFAULT_LIMITS) -> None:
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        self.max_workers = max_workers
        self.limits = limits
        self._mp_ctx = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._idle: list[_WorkerSlot] = []
        self._active: set[_WorkerSlot] = set()
        self._spawning = 0
        self._closed = False

    def _spawn_slot(self) -> _WorkerSlot:
        parent_conn, child_conn = self._mp_ctx.Pipe(duplex=True)
        proc = self._mp_ctx.Process(
            target=_worker_main,
            args=(child_conn,),
            name="commissure-parse-worker",
            daemon=False,
        )
        proc.start()
        # Close our copy of `child_conn` immediately so child exit delivers instant EOF.
        child_conn.close()
        return _WorkerSlot(proc=proc, conn=parent_conn)

    def _acquire_slot(self, cancel_event: threading.Event | None) -> _WorkerSlot:
        dead_slots: list[_WorkerSlot] = []
        spawn_needed = False
        slot: _WorkerSlot | None = None
        try:
            with self._cond:
                while True:
                    if self._closed:
                        raise RunCancelled("parse pool closed")
                    if cancel_event is not None and cancel_event.is_set():
                        raise RunCancelled("parse cancelled")
                    while self._idle:
                        candidate = self._idle.pop()
                        if candidate.proc.is_alive():
                            slot = candidate
                            self._active.add(slot)
                            break
                        dead_slots.append(candidate)
                    if slot is not None:
                        break
                    if len(self._active) + self._spawning < self.max_workers:
                        self._spawning += 1
                        spawn_needed = True
                        break
                    self._cond.wait(timeout=_POLL_SLICE_SECONDS)
        finally:
            for dead in dead_slots:
                _terminate_slot(dead)

        if not spawn_needed:
            assert slot is not None
            return slot

        spawned: _WorkerSlot | None = None
        try:
            spawned = self._spawn_slot()
            with self._cond:
                self._spawning -= 1
                if self._closed or (cancel_event is not None and cancel_event.is_set()):
                    self._cond.notify_all()
                    raise RunCancelled("parse cancelled")
                self._active.add(spawned)
                return spawned
        except BaseException:
            if spawned is not None:
                _terminate_slot(spawned)
            else:
                with self._cond:
                    self._spawning -= 1
                    self._cond.notify_all()
            raise

    def _release_slot(self, slot: _WorkerSlot, *, recycle: bool = False) -> None:
        kill_now = False
        with self._cond:
            self._active.discard(slot)
            if self._closed or recycle or not slot.proc.is_alive():
                kill_now = True
            else:
                self._idle.append(slot)
            self._cond.notify_all()
        if kill_now:
            _terminate_slot(slot, graceful=not self._closed)

    def parse_item(
        self,
        parser_name: str,
        blob_path: Path,
        *,
        cancel_event: threading.Event | None = None,
        _test_sleep_s: float = 0.0,
    ) -> RecordEnvelope:
        """Execute `parser_name` on `blob_path` with preemptive timeout and cancellation."""
        timeout_s = (
            self.limits.max_ocr_seconds if parser_name == "ocr" else self.limits.max_parse_seconds
        )
        slot = self._acquire_slot(cancel_event)
        t0 = time.monotonic()
        deadline = t0 + timeout_s
        try:
            slot.conn.send((parser_name, str(blob_path), self.limits, _test_sleep_s))
            while True:
                if self._closed or (cancel_event is not None and cancel_event.is_set()):
                    self._release_slot(slot, recycle=True)
                    raise RunCancelled("parse interrupted")
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    elapsed = round(now - t0, 3)
                    self._release_slot(slot, recycle=True)
                    return RecordEnvelope.quarantined(
                        "parse_timeout",
                        Diagnostic(
                            code="parse_timeout",
                            message=(
                                f"parser {parser_name!r} timed out after {elapsed:.2f}s"
                                f" (limit {timeout_s:g}s)"
                            ),
                            detail={"actual": elapsed, "limit": timeout_s},
                        ),
                    )
                if slot.conn.poll(min(_POLL_SLICE_SECONDS, remaining)):
                    env: RecordEnvelope = slot.conn.recv()
                    break
        except RunCancelled:
            raise
        except (EOFError, BrokenPipeError, OSError) as exc:
            if self._closed or (cancel_event is not None and cancel_event.is_set()):
                self._release_slot(slot, recycle=True)
                raise RunCancelled("parse interrupted") from None
            exit_code = slot.proc.exitcode
            self._release_slot(slot, recycle=True)
            detail_msg = (
                f"parse worker exited unexpectedly (exitcode={exit_code})"
                if exit_code is not None
                else f"parse worker connection lost: {type(exc).__name__}: {exc}"
            )
            return RecordEnvelope.quarantined(
                "parse_error",
                Diagnostic(code="parse_error", message=detail_msg),
            )
        except BaseException:
            self._release_slot(slot, recycle=True)
            raise

        slot.tasks_completed += 1
        large_payload = False
        if isinstance(env.payload, tuple) and len(env.payload) == 3:
            text_s, _, struct_s = env.payload
            if isinstance(text_s, str) and isinstance(struct_s, str):
                large_payload = (len(text_s) + len(struct_s)) >= RECYCLE_PAYLOAD_BYTES
        recycle = slot.tasks_completed >= MAX_TASKS_PER_WORKER or large_payload
        self._release_slot(slot, recycle=recycle)
        return env

    def close(self) -> None:
        """Terminate all idle and active worker processes and close pipes idempotently."""
        with self._cond:
            if self._closed and not self._idle and not self._active:
                return
            self._closed = True
            to_close = [*self._idle, *self._active]
            self._idle.clear()
            self._active.clear()
            self._cond.notify_all()
        for slot in to_close:
            _terminate_slot(slot, graceful=False)
