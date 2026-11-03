"""Probe against real files in tests/fixtures/real_samples, checked against the
reviewed expectations in tests/fixtures/real_samples.expected.json. The manifest lives
outside the folder so probing or ingesting the folder sees only samples."""

import json
from pathlib import Path

import pytest

from commissure.probe import probe_path

SAMPLES = Path(__file__).parent / "fixtures" / "real_samples"
MANIFEST = json.loads((SAMPLES.parent / "real_samples.expected.json").read_text())["files"]


@pytest.fixture(scope="module")
def probed():
    return {f["locator"]: f for f in probe_path(SAMPLES)["files"]}


def test_every_sample_has_reviewed_expectations():
    on_disk = {p.name for p in SAMPLES.iterdir() if p.is_file()}
    assert on_disk == set(MANIFEST)


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_real_sample_matches_expectations(probed, name):
    expected, entry = MANIFEST[name], probed[name]
    assert entry["status"] == expected.get("status", "inspected"), entry.get("error")
    if "pdf" in expected:
        pdf = entry["pdf"]
        want = expected["pdf"]
        got = {k: pdf[k] for k in ("pages", "readable", "unreadable", "blank")}
        assert got == {k: want[k] for k in got}
        flags = {str(p["page"]): p["flags"] for p in pdf["page_details"] if p["flags"]}
        assert flags == want["flags"]
        reasons = {str(p["page"]): p["reasons"] for p in pdf["page_details"] if p["reasons"]}
        assert reasons == want.get("reasons", {})
        if "font_subtypes" in want:
            subtypes = sorted({f["subtype"] for f in pdf["fonts"] if f["subtype"]})
            assert subtypes == want["font_subtypes"]
    if "docx" in expected:
        d = entry["docx"]
        got = {
            "paragraphs": d["paragraphs"],
            "headings": len(d["headings"]),
            "heading_levels": sorted({h["level"] for h in d["headings"]}),
            "tables": len(d["tables"]),
            "heading_level_skips": len(d["flags"]["heading_level_skips"]),
            "possible_unstyled_headings": len(d["flags"]["possible_unstyled_headings"]),
        }
        assert got == expected["docx"]
    if "xlsx" in expected:
        sheets = [
            {"name": s["name"], "header_row": s["header_row"], "data_rows": s["data_rows"],
             "columns": len(s["columns"]), "flags": s["flags"]}
            for s in entry["xlsx"]["sheets"]
        ]  # fmt: skip
        assert sheets == expected["xlsx"]
