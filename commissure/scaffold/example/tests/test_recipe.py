"""The example end to end, through the same CLI a person runs, over a copy of the corpus.

The counts are the acceptance gate in recipe/AGENTS.md. If a change to the recipe moves one,
the change is either a fix (update the counts and the contract together) or a regression.
"""

import json
import shutil
from pathlib import Path

import pytest
from commissure.cli import app
from commissure.workspace import open_workspace
from typer.testing import CliRunner

from recipe.extract import PHRASES

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("example")
    corpus = tmp / "comments"
    shutil.copytree(ROOT / "intake" / "comments", corpus)  # originals are never touched
    ws = tmp / "workspace"
    runner = CliRunner()
    steps = [
        ["--step", "ingest", "--source", str(corpus), "--role", "comment"],
        ["--step", "parse"],
        ["--step", "extract"],
        ["--step", "tally"],
    ]
    reports = {}
    for step in steps:
        args = ["run", *step, "--workspace", str(ws), "--recipe", str(ROOT / "recipe"), "--json"]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        reports[step[1]] = json.loads(result.stdout)
    return ws, reports


def test_outcomes_match_the_acceptance_gate(workspace):
    _, reports = workspace
    parse, extract = reports["parse"], reports["extract"]
    assert (parse["valid"], parse["quarantined"]) == (22, 1)  # the scan has no text layer
    assert (extract["valid"], extract["warning"], extract["quarantined"], extract["skipped"]) == (
        20,
        1,  # c09: no position phrase
        1,  # c10: both support and opposition
        1,  # the scan, set aside at parse
    )


def test_tally_counts_and_names_what_was_set_aside(workspace):
    ws, _ = workspace
    w = open_workspace(ws)
    try:
        row = w.conn.execute(
            "SELECT payload FROM current_records WHERE stage = 'recipe.tally'"
        ).fetchone()
    finally:
        w.close()
    tally = json.loads(row[0])
    assert (tally["comments"], tally["support"], tally["oppose"], tally["no_position"]) == (
        21,
        12,
        8,
        1,
    )
    assert tally["set_aside"] == ["c10.html", "c23-scan.pdf"]


def test_every_position_rests_on_a_phrase_from_the_list(workspace):
    """Evidence is checked by core; this checks it is the evidence the contract promises."""
    ws, _ = workspace
    w = open_workspace(ws)
    try:
        rows = w.conn.execute(
            "SELECT payload FROM current_records WHERE stage = 'recipe.extract'"
        ).fetchall()
    finally:
        w.close()
    positions = [json.loads(r[0]) for r in rows]
    assert len(positions) == 21
    for p in positions:
        if p["position"] is None:
            assert p["position_evidence"] is None
            continue
        quote = p["position_evidence"]["text"]
        assert PHRASES[p["position"]].fullmatch(quote), quote
    # The two-page letter's position is on its second page, and core says so.
    evidence = [p["position_evidence"] for p in positions if p["position_evidence"]]
    pages = {ev["locator"]: ev["page"] for ev in evidence}
    assert pages["c21.pdf"] == 2
