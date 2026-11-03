"""Progress snapshot formatting, CLI stderr progress, and ETA gating."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from commissure.progress import (
    ProgressReporter,
    ProgressSnapshot,
    estimate_remaining_seconds,
    format_progress_line,
)
from commissure.serve import BusyStatus, _make_busy_status
from commissure.workspace import open_workspace, utc_now

DEMO = Path(__file__).parent / "recipes" / "demo"


def _minute_ago() -> str:
    """A run start 60s back, so the ledger has elapsed time to compute a rate from."""
    ts = datetime.now(UTC) - timedelta(seconds=60)
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_eta_withheld_until_two_completions() -> None:
    """One completion is not a rate; the estimate appears only at done >= 2."""
    assert estimate_remaining_seconds(0, 10, 5.0) is None
    assert estimate_remaining_seconds(1, 10, 5.0) is None
    assert estimate_remaining_seconds(2, 10, 4.0) == 16.0
    line_one = format_progress_line(
        ProgressSnapshot(label="parse", done=1, total=10, elapsed_seconds=5.0)
    )
    assert "remaining" not in line_one
    line_two = format_progress_line(
        ProgressSnapshot(label="parse", done=2, total=10, elapsed_seconds=4.0)
    )
    assert "remaining" in line_two


def test_progress_reporter_emits_immediately_when_warnings_rise() -> None:
    lines: list[str] = []
    clock = {"t": 0.0}
    reporter = ProgressReporter(lines.append, min_interval_s=10.0, clock=lambda: clock["t"])
    reporter.report(ProgressSnapshot(label="s", done=0, total=5, elapsed_seconds=0.0))
    clock["t"] = 0.1
    reporter.report(ProgressSnapshot(label="s", done=1, total=5, elapsed_seconds=0.1, warning=0))
    assert len(lines) == 1  # throttled
    clock["t"] = 0.2
    reporter.report(ProgressSnapshot(label="s", done=2, total=5, elapsed_seconds=0.2, warning=1))
    assert len(lines) == 2
    assert "warnings=1" in lines[-1]


def test_run_json_keeps_progress_off_stdout(cli, ws_root, tmp_path) -> None:
    """Progress text must stay on stderr; printing it to stdout fails this test."""
    src = tmp_path / "src"
    src.mkdir()
    for name, amount in (("alpha", 5), ("bravo", 3), ("apple", 7)):
        (src / f"{name}.json").write_text(json.dumps({"name": name, "amount": amount}))
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root), "--json").exit_code == 0

    result = cli(
        "run",
        "--step",
        "extract",
        "--workspace",
        str(ws_root),
        "--recipe",
        str(DEMO),
        "--progress",
        "--json",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "discovered" in payload
    # Stdout must be pure JSON — no progress prose mixed in.
    assert "elapsed" not in result.stdout
    assert "warnings=" not in result.stdout
    assert "current=" not in result.stdout
    stderr = result.stderr or ""
    assert "extract" in stderr
    assert "/" in stderr  # done/total
    # The final line survives the throttle.
    assert "3/3" in stderr.splitlines()[-1]


def test_run_progress_is_off_when_stderr_is_not_a_terminal(cli, ws_root, tmp_path) -> None:
    """A cron run (stderr not a TTY, no flag) stays quiet; `--progress` forces it on."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.json").write_text(json.dumps({"name": "alpha", "amount": 5}))
    assert cli.ingest(ws_root, src)[0] == 0
    result = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json")
    assert result.exit_code == 0, result.output
    assert "elapsed" not in (result.stderr or "")


def test_eta_leaves_plan_time_skips_out_of_the_rate() -> None:
    """900 instant skips plus 2 real subjects in 60s: about 49 minutes left, not seconds."""
    snap = ProgressSnapshot(label="s", done=902, total=1000, elapsed_seconds=60.0, skipped=900)
    assert snap.remaining_seconds == 30.0 * 98
    unskipped = ProgressSnapshot(label="s", done=902, total=1000, elapsed_seconds=60.0)
    assert unskipped.remaining_seconds is not None and unskipped.remaining_seconds < 10


def test_progress_reporter_finish_emits_the_throttled_last_line() -> None:
    lines: list[str] = []
    reporter = ProgressReporter(lines.append, min_interval_s=10.0, clock=lambda: 0.0)
    reporter.report(ProgressSnapshot(label="s", done=0, total=2, elapsed_seconds=0.0))
    reporter.report(ProgressSnapshot(label="s", done=2, total=2, elapsed_seconds=0.1))
    assert len(lines) == 1
    reporter.finish()
    assert len(lines) == 2 and "2/2" in lines[-1]
    reporter.finish()
    assert len(lines) == 2


def test_busy_status_shows_warnings_quarantine_and_eta() -> None:
    """Busy bar snapshot carries live warning/quarantine counts and a time remaining."""
    ledger = {
        "step_name": "parse",
        "started_at": utc_now(),
        "done": 4,
        "total": 10,
        "percent": 40,
        "warning": 2,
        "quarantined": 1,
        "remaining_display": "01:30",
        "last_activity_seconds": 3,
    }
    busy = _make_busy_status(
        source="external",
        in_process_label=None,
        in_process_elapsed_seconds=None,
        external_since=None,
        ledger_info=ledger,
    )
    assert isinstance(busy, BusyStatus)
    assert busy.warning == 2
    assert busy.quarantined == 1
    assert busy.remaining_display == "01:30"
    assert busy.done == 4
    assert busy.total == 10

    # Fetch with no ledger: in-process done/total fill the bar.
    fetch_busy = _make_busy_status(
        source="interface",
        in_process_label="Fetching comments",
        in_process_elapsed_seconds=20,
        external_since=None,
        ledger_info=None,
        in_process_done=4,
        in_process_total=10,
    )
    assert fetch_busy.done == 4
    assert fetch_busy.total == 10
    assert fetch_busy.remaining_display is not None


def test_inspect_running_ledger_groups_outcome_counts(ws_root: Path) -> None:
    from commissure.runs import start_run
    from commissure.serve import _inspect_running_ledger
    from commissure.workspace import transaction

    ws = open_workspace(ws_root)
    try:
        run_id = start_run(
            ws.conn,
            "demo.extract",
            trigger="test",
            config={"limit": {"value": 10, "from": "flag"}},
            stage_fp="fp",
        )
        with transaction(ws.conn):
            ws.conn.execute(
                "UPDATE runs SET discovered = 10, remaining = 10, started_at = ? WHERE id = ?",
                (_minute_ago(), run_id),
            )
            for i, (outcome, reason) in enumerate(
                (
                    ("valid", None),
                    ("valid", None),
                    ("warning", "soft"),
                    ("quarantined", "hard"),
                    ("valid", None),
                ),
                start=1,
            ):
                record_id = None
                if outcome in ("valid", "warning"):
                    cur = ws.conn.execute(
                        "INSERT INTO records (stage, payload, payload_hash, created_at)"
                        " VALUES ('demo.extract', '{}', ?, ?)",
                        (f"hash-{i}", utc_now()),
                    )
                    record_id = cur.lastrowid
                ws.conn.execute(
                    "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key,"
                    " input_fp, stage_fp, params, outcome, reason_code, diagnostics,"
                    " record_id, created_at)"
                    " VALUES (?, 'demo.extract', 'run', ?, 'ifp', 'fp', '{}', ?, ?, '[]',"
                    " ?, ?)",
                    (run_id, f"run:{i}", outcome, reason, record_id, utc_now()),
                )
        info = _inspect_running_ledger(ws.conn, "demo")
        assert info is not None
        assert info["done"] == 5
        assert info["total"] == 10
        assert info["warning"] == 1
        assert info["quarantined"] == 1
        assert info["remaining_display"] is not None
    finally:
        ws.close()


def test_inspect_running_ledger_eta_leaves_skips_out_of_the_rate(ws_root: Path) -> None:
    """Three instant skips and one real outcome: one completion is not a rate."""
    from commissure.runs import start_run
    from commissure.serve import _inspect_running_ledger
    from commissure.workspace import transaction

    ws = open_workspace(ws_root)
    try:
        run_id = start_run(ws.conn, "demo.extract", trigger="test", config={}, stage_fp="fp")
        with transaction(ws.conn):
            ws.conn.execute(
                "UPDATE runs SET discovered = 10, remaining = 10, started_at = ? WHERE id = ?",
                (_minute_ago(), run_id),
            )
            for i, outcome in enumerate(("skipped", "skipped", "skipped", "quarantined")):
                ws.conn.execute(
                    "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key,"
                    " input_fp, stage_fp, params, outcome, reason_code, diagnostics,"
                    " record_id, created_at)"
                    " VALUES (?, 'demo.extract', 'run', ?, 'ifp', 'fp', '{}', ?, 'r', '[]',"
                    " NULL, ?)",
                    (run_id, f"run:{i}", outcome, utc_now()),
                )
        info = _inspect_running_ledger(ws.conn, "demo")
        assert info is not None and info["done"] == 4
        assert info["remaining_display"] is None
    finally:
        ws.close()


def test_progress_names_the_oldest_in_flight_subject_with_workers(cli, ws_root, tmp_path) -> None:
    """With several workers, `current` is the subject running longest, not the last submitted.

    `a_slow` is submitted first and still runs while `b` and `c` finish; every snapshot in
    that span must name it.
    """
    from commissure.recipes import load_recipe, recipe_plan, register_stages
    from commissure.runner import run_plan

    src = tmp_path / "src"
    src.mkdir()
    for name in ("a_slow", "b", "c"):
        (src / f"{name}.json").write_text(json.dumps({"name": name}))
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root), "--json").exit_code == 0
    recipe_dir = tmp_path / "slow_recipe"
    recipe_dir.mkdir()
    (recipe_dir / "recipe.py").write_text(
        "import time\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n\n"
        "class Out(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    ok: bool = True\n\n"
        "def work(ctx, item):\n"
        "    if 'a_slow' in item.locator:\n"
        "        time.sleep(0.6)\n"
        "    return Out()\n\n"
        "STAGES = [Stage('work', work, schema=Out, text='default')]\n",
        encoding="utf-8",
    )
    ws = open_workspace(ws_root)
    try:
        loaded = load_recipe(recipe_dir)
        register_stages(ws.conn, loaded)
        plan = recipe_plan(ws, loaded, "work")
        first = plan.subjects()[0]
        assert "a_slow" in first.input.locator
        snaps: list[ProgressSnapshot] = []
        run_plan(ws, plan, trigger="test", config={}, workers=2, on_progress=snaps.append)
    finally:
        ws.close()
    while_slow_runs = [s for s in snaps if 1 <= s.done < 3]
    assert while_slow_runs
    assert {s.current for s in while_slow_runs} == {first.key}
