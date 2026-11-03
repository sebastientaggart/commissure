"""Shared progress snapshot for `commissure run` and `commissure serve`.

Domain-neutral: a label, done/total counts, elapsed time, and optional warning /
quarantine counts. Estimated time remaining uses the completed rate and is
withheld until at least two subjects have finished — one completion is not a
rate (the serve busy bar already waits for `to_do >= 2` before showing a
percentage for the same reason).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressSnapshot:
    """Live progress for one long operation (a stage run or a recipe-local fetch)."""

    label: str
    done: int
    total: int
    elapsed_seconds: float
    warning: int = 0
    quarantined: int = 0
    current: str | None = None
    # Subjects written without running (plan-time skips), counted in `done`. They take no
    # time, so the rate leaves them out; 900 instant skips would otherwise turn an
    # hour-long estimate into seconds.
    skipped: int = 0

    @property
    def remaining_seconds(self) -> float | None:
        return estimate_remaining_seconds(
            self.done - self.skipped, self.total - self.skipped, self.elapsed_seconds
        )


def estimate_remaining_seconds(done: int, total: int, elapsed_seconds: float) -> float | None:
    """Return a rate-based ETA, or `None` until two completions establish a rate.

    One completion is not a rate: a single slow (or cached) subject would dominate
    the estimate. Require `done >= 2` and at least one subject still pending.
    """
    if done < 2 or total <= done or elapsed_seconds <= 0:
        return None
    rate = elapsed_seconds / done
    return rate * (total - done)


def format_elapsed(seconds: float) -> str:
    clamped = max(0, int(seconds))
    hrs, rem = divmod(clamped, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_remaining(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    # Round up to whole seconds so a fractional remainder never shows as 00:00.
    return format_elapsed(max(1, int(seconds + 0.999)))


def format_progress_line(snap: ProgressSnapshot) -> str:
    """One human-readable progress line for stderr (never stdout / `--json`)."""
    parts = [snap.label.strip() or "running", f"{snap.done}/{snap.total}"]
    parts.append(f"{format_elapsed(snap.elapsed_seconds)} elapsed")
    eta = format_remaining(snap.remaining_seconds)
    if eta is not None:
        parts.append(f"~{eta} remaining")
    if snap.total > 0 or snap.warning or snap.quarantined:
        # Include zeros so a run going badly is comparable to one going well.
        parts.append(f"warnings={snap.warning} quarantined={snap.quarantined}")
    if snap.current:
        parts.append(f"current={snap.current}")
    return " ".join(parts)


class ProgressReporter:
    """Throttle progress lines to stderr: at most one per second, unless findings rise.

    Call `finish()` when the operation ends so the last line is never lost to the throttle.
    """

    def __init__(
        self,
        emit: Callable[[str], None],
        *,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self._last_warning = 0
        self._last_quarantined = 0
        self._last_line: str | None = None
        self._pending_line: str | None = None

    def report(self, snap: ProgressSnapshot) -> None:
        line = format_progress_line(snap)
        with self._lock:
            findings_up = (
                snap.warning > self._last_warning or snap.quarantined > self._last_quarantined
            )
            now = self._clock()
            due = (now - self._last_emit) >= self._min_interval_s
            if not findings_up and not due and self._last_line is not None:
                self._pending_line = line if line != self._last_line else None
                return
            if line == self._last_line and not findings_up:
                return
            self._last_emit = now
            self._last_warning = snap.warning
            self._last_quarantined = snap.quarantined
            self._last_line = line
            self._pending_line = None
        self._emit(line)

    def finish(self) -> None:
        """Emit the latest line if the throttle held it back."""
        with self._lock:
            line, self._pending_line = self._pending_line, None
            if line is not None:
                self._last_line = line
        if line is not None:
            self._emit(line)
