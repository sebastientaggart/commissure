"""`commissure.testing`: public test fixtures and `Pipeline` helper for solution tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from commissure.scaffold import scaffold_solution
from commissure.testing import Pipeline
from commissure.workspace import open_workspace

DEMO = Path(__file__).parent / "recipes" / "demo"


def test_example_solution_runs_with_pipeline_and_records_match_cli(tmp_path):
    sol = tmp_path / "example"
    scaffold_solution(sol, kind="example")

    run = Pipeline(tmp_path / "ws", sol / "recipe")
    ing = run.ingest(sol / "intake" / "comments", role="comment")
    assert ing.exit_code == 0 and ing["exit_code"] == 0
    assert ing["added"] == 23

    # Originals in sol/intake/comments were copied into tmp_path/intake before ingest,
    # so the workspace's stored source points at the staged copy, not the original folder.
    ws = open_workspace(run.workspace)
    try:
        stored_uris = [r["uri"] for r in ws.conn.execute("SELECT uri FROM sources")]
    finally:
        ws.close()
    assert all(
        not Path(uri).is_absolute() or not Path(uri).is_relative_to(sol / "intake")
        for uri in stored_uris
    )

    reports = run.steps("parse", "extract", "tally")
    assert [r.exit_code for r in reports.values()] == [0, 0, 0]

    # .records("tally") returns the exact payload the CLI wrote into current_records.
    tally_records = run.records("tally")
    assert list(tally_records) == ["run"]
    tally_rec = tally_records["run"]
    assert tally_rec.outcome == "valid" and tally_rec["outcome"] == "valid"
    assert tally_rec.reason is None and tally_rec.codes == []

    ws = open_workspace(run.workspace)
    try:
        raw_tally = json.loads(
            ws.conn.execute(
                "SELECT payload FROM current_records WHERE stage = 'recipe.tally'"
            ).fetchone()[0]
        )
    finally:
        ws.close()
    assert tally_rec.payload == tally_rec["payload"] == raw_tally
    assert (
        raw_tally["comments"],
        raw_tally["support"],
        raw_tally["oppose"],
        raw_tally["no_position"],
    ) == (21, 12, 8, 1)

    # .records("extract") reports valid, warning, quarantined, and the scan skipped at parse.
    extract_records = run.records("extract")
    assert len(extract_records) == 23

    # warning: c09.html has no position phrase
    c09 = extract_records["c09.html"]
    assert c09.outcome == "warning"
    assert c09.reason == "no_position_phrase"
    assert c09.codes == ["no_position_phrase"]
    assert c09.payload is not None and c09.payload["position"] is None

    # quarantined: c10.html has both support and opposition
    c10 = extract_records["c10.html"]
    assert c10.outcome == "quarantined"
    assert c10.reason == "conflicting_position"
    assert c10.codes == ["conflicting_position"]
    assert c10.payload is None

    # skipped (not missing!): c23-scan.pdf was quarantined at parse
    scan = extract_records["c23-scan.pdf"]
    assert scan.outcome == "skipped"
    assert scan.reason == "no_text"
    assert scan.codes == ["no_text"]
    assert scan.payload is None

    # parse outcomes are readable through .records("parse") too
    parse_records = run.records("parse")
    assert parse_records["c23-scan.pdf"].outcome == "quarantined"
    assert "no_readable_text" in (parse_records["c23-scan.pdf"].reason or "")
    assert "unreadable_pages" in parse_records["c23-scan.pdf"].codes


def test_group_records_and_review_subcommands(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "good.json").write_text(json.dumps({"name": "good", "amount": 10}), encoding="utf-8")
    (src / "great.json").write_text(json.dumps({"name": "great", "amount": 5}), encoding="utf-8")
    (src / "bad.json").write_text(json.dumps({"name": "bad", "amount": -3}), encoding="utf-8")

    run = Pipeline(tmp_path / "workspace", DEMO, reviewer="alice")
    assert run.ingest(src).exit_code == 0
    assert run.parse().exit_code == 0
    assert run.stage("extract").exit_code == 0
    assert run.stage("by_initial").exit_code == 0

    ext = run.records("extract")
    assert ext["bad.json"] == {
        "outcome": "quarantined",
        "reason": "negative_amount",
        "codes": ["negative_amount"],
        "payload": None,
    }
    groups = run.records("by_initial")
    assert list(groups) == ["g"]
    assert groups["g"].outcome == "valid"
    assert groups["g"].payload == {"key": "g", "count": 2, "total": 15, "excluded": []}

    # .review() attaches --recipe and --reviewer only on the subcommands that accept them.
    checks = run.review("checks")
    assert checks.exit_code == 0
    assert any(c["id"] == "extract:/amount" for c in checks["checks"])

    sample = run.review("sample", "--check", "extract:/amount", "--n", "1", "--seed", "42")
    assert sample.exit_code == 0 and sample["n"] == 1

    nxt = run.review("next")
    assert nxt.exit_code == 0
    inst_id = nxt["instance"]["instance_id"]

    rec = run.review("record", str(inst_id), "correct", "--note", "verified")
    assert rec.exit_code == 0 and rec["instance_id"] == inst_id

    spot = run.review(
        "spot", "--check", "extract:/name", "--subject", "good.json", "--pointer", "/name"
    )
    assert spot.exit_code == 0 and spot["instance"]["pointer"] == "/name"

    rep = run.review("report")
    assert rep.exit_code == 0
    assert rep["samples"][0]["correct"] == 1
    assert rep["samples"][0]["reviewer"] == "alice"


def test_failing_stage_exit_code_reaches_the_test(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "only_bad.json").write_text(json.dumps({"name": "bad", "amount": -10}), encoding="utf-8")

    run = Pipeline(tmp_path / "workspace", DEMO)
    assert run.ingest(src).exit_code == 0
    assert run.parse().exit_code == 0

    # All items quarantined fails the run with exit code 1; Pipeline returns it without raising.
    failed = run.stage("extract")
    assert failed.exit_code == 1 and failed["exit_code"] == 1
    assert failed["status"] == "failed"
    assert failed["quarantined"] == 1

    # Usage error (unknown stage) returns exit code 2 without raising.
    usage = run.stage("no_such_stage")
    assert usage.exit_code == 2 and usage["exit_code"] == 2
    assert "no stage" in usage.stderr


def test_commissure_run_pytest_fixture_is_registered(commissure_run, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "item.json").write_text(json.dumps({"name": "ok", "amount": 7}), encoding="utf-8")

    commissure_run.recipe = DEMO
    assert commissure_run.workspace.parent == tmp_path
    assert commissure_run.load().name == "demo"
    assert commissure_run.ingest(src).exit_code == 0
    reports = commissure_run.steps("parse", "extract")
    assert [r.exit_code for r in reports.values()] == [0, 0]
    assert commissure_run.records("extract")["item.json"].payload == {"name": "ok", "amount": 7}


def test_importing_commissure_and_api_does_not_import_pytest():
    code = (
        "import sys, commissure, commissure.api; "
        "assert 'pytest' not in sys.modules, list(sys.modules); "
        "assert 'commissure.testing' not in sys.modules"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
