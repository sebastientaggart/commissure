"""The example end to end, through the same CLI a person runs, over a copy of the corpus.

The counts are the acceptance gate in AGENTS.md. If a change to the recipe moves one,
the change is either a fix (update the counts and the contract together) or a regression.
"""

from pathlib import Path

import pytest
from commissure.testing import Pipeline

from recipe.extract import PHRASES

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    run = Pipeline(tmp_path_factory.mktemp("example") / "workspace", ROOT / "recipe")
    assert run.ingest(ROOT / "intake" / "comments", role="comment").exit_code == 0
    for report in run.steps("parse", "extract", "tally").values():
        assert report.exit_code == 0, report
    return run


def test_outcomes_match_the_acceptance_gate(pipeline):
    parse, extract = pipeline.reports["parse"], pipeline.reports["extract"]
    assert (parse["valid"], parse["quarantined"]) == (22, 1)  # the scan has no text layer
    assert (extract["valid"], extract["warning"], extract["quarantined"], extract["skipped"]) == (
        20,
        1,  # c09: no position phrase
        1,  # c10: both support and opposition
        1,  # the scan, set aside at parse
    )
    records = pipeline.records("extract")
    assert (records["c09.html"].outcome, records["c09.html"].reason) == (
        "warning",
        "no_position_phrase",
    )
    assert (records["c10.html"].outcome, records["c10.html"].reason) == (
        "quarantined",
        "conflicting_position",
    )
    assert (records["c23-scan.pdf"].outcome, records["c23-scan.pdf"].reason) == (
        "skipped",
        "no_text",
    )


def test_tally_counts_and_names_what_was_set_aside(pipeline):
    tally = pipeline.records("tally")["run"].payload
    assert (tally["comments"], tally["support"], tally["oppose"], tally["no_position"]) == (
        21,
        12,
        8,
        1,
    )
    assert tally["set_aside"] == ["c10.html", "c23-scan.pdf"]


def test_every_position_rests_on_a_phrase_from_the_list(pipeline):
    """Evidence is checked by core; this checks it is the evidence the contract promises."""
    positions = [r.payload for r in pipeline.records("extract").values() if r.payload is not None]
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
