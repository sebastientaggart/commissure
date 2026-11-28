"""Unit tests for commissure/patterns/citations.py."""

from __future__ import annotations

import shutil
import tomllib
from datetime import date
from pathlib import Path

import pytest

from commissure.patterns import (
    CFR_RE,
    CFR_SECTION_RE,
    COURT_CASE_RE,
    DOCKET_RE,
    FED_REG_RE,
    FEDERAL_CITATION_PATTERNS,
    LOOSE_DATE_RE,
    MONTHS,
    NUMERIC_DATE_RE,
    PROSE_DATE_RE,
    PUBLIC_LAW_RE,
    STAT_RE,
    USC_RE,
    canonical,
    canonical_cfr,
    canonical_citation,
    canonical_docket,
    canonical_fed_reg,
    canonical_public_law,
    canonical_stat,
    canonical_usc,
    parse_date,
    parse_numeric_date,
    parse_prose_date,
)


def test_cfr_and_cfr_section_regex_and_canonical() -> None:
    sample = "See 45 C.F.R. § 1355.22, 2 CFR §§ 200.300, 45 CFR Part 1355, and 45 CFR Parts 1301."
    matches = [canonical_cfr(m) for m in CFR_RE.finditer(sample)]
    assert matches == ["45 CFR 1355.22", "2 CFR 200.300", "45 CFR 1355", "45 CFR 1301"]

    sec_matches = [canonical("cfr", m) for m in CFR_SECTION_RE.finditer(sample)]
    assert sec_matches == ["45 CFR 1355.22", "2 CFR 200.300"]


def test_usc_public_law_fed_reg_stat_and_docket_canonicalizers() -> None:
    usc_text = "Authorized by 42 U.S.C. § 671, 42 U. S. C. 9836a, and 42 U.S.C. 670 – 679b."
    assert [canonical_usc(m) for m in USC_RE.finditer(usc_text)] == [
        "42 U.S.C. 671",
        "42 U.S.C. 9836a",
        "42 U.S.C. 670-679b",
    ]

    pl_text = (
        "Enacted in Public Law 115-123, Pub. L. 110–351, P.L. 105—89, and Public Law (P.L.) 96-272."
    )
    assert [canonical_public_law(m) for m in PUBLIC_LAW_RE.finditer(pl_text)] == [
        "P.L. 115-123",
        "P.L. 110-351",
        "P.L. 105-89",
        "P.L. 96-272",
    ]

    fr_text = "Published at 89 FR 34818 and 89 Fed. Reg. 67,720."
    assert [canonical_fed_reg(m) for m in FED_REG_RE.finditer(fr_text)] == [
        "89 FR 34818",
        "89 FR 67720",
    ]

    stat_text = "Printed at 121 Stat. 1363 and 132 STAT. 2,345."
    assert [canonical_stat(m) for m in STAT_RE.finditer(stat_text)] == [
        "121 Stat. 1363",
        "132 Stat. 2345",
    ]

    case_text = "Stayed in State v. Department, Case No. 6:24-cv-348 (see also No. 1:23-cv-0042)."
    case_m = COURT_CASE_RE.search(case_text)
    assert case_m is not None and canonical_docket(case_m) == "6:24-cv-348"
    assert [canonical_citation("court_case", m) for m in DOCKET_RE.finditer(case_text)] == [
        "6:24-cv-348",
        "1:23-cv-0042",
    ]

    found = {
        (kind, canonical(kind, m))
        for kind, pat in FEDERAL_CITATION_PATTERNS
        for m in pat.finditer(f"{usc_text} {pl_text} {fr_text} {stat_text} {case_text}")
    }
    assert ("usc", "42 U.S.C. 671") in found
    assert ("public_law", "P.L. 115-123") in found
    assert ("fed_reg", "89 FR 67720") in found
    assert ("stat", "121 Stat. 1363") in found
    assert ("court_case", "6:24-cv-348") in found

    with pytest.raises(ValueError, match="unknown citation kind"):
        canonical("unknown_kind", case_m)


def test_prose_and_numeric_date_parsing_and_invalid_calendar_dates() -> None:
    assert len(MONTHS) == 12
    assert PROSE_DATE_RE.search("March 3, 2024") is not None
    assert LOOSE_DATE_RE.search("December 20, eos . 2024") is not None
    assert NUMERIC_DATE_RE.search("11-19-25") is not None

    assert parse_prose_date("Issued March 3, 2024.") == date(2024, 3, 3)
    assert parse_prose_date("Effective Sept. 1st, 2023") == date(2023, 9, 1)
    assert parse_prose_date("DECEMBER 20, 2024") == date(2024, 12, 20)

    noisy = "Issuance Date: December 20, eos . 2024"
    assert parse_prose_date(noisy, loose=False) is None
    assert parse_prose_date(noisy, loose=True) == date(2024, 12, 20)
    assert parse_date(noisy, loose=False) is None
    assert parse_date(noisy, loose=True) == date(2024, 12, 20)

    assert parse_numeric_date("11-19-25") == date(2025, 11, 19)
    assert parse_numeric_date("6/18/2026") == date(2026, 6, 18)
    assert parse_numeric_date("04/15/97", pivot_year=50) == date(1997, 4, 15)
    assert parse_numeric_date("04/15/24", pivot_year=50) == date(2024, 4, 15)
    assert parse_date("04/15/97", pivot_year=50) == date(1997, 4, 15)

    # Invalid calendar dates must return None rather than raising or clamping.
    for bad in ("February 30, 2024", "June 31, 2024", "2/30/2024", "13/01/2024", "no date"):
        assert parse_date(bad) is None
        assert parse_prose_date(bad) is None
        assert parse_numeric_date(bad) is None


def test_patterns_package_passes_release_forbidden_scan(tmp_path: Path) -> None:
    from commissure.tools import release

    root = Path(__file__).resolve().parents[1]
    patterns_dir = root / "commissure" / "patterns"
    tree = tmp_path / "patterns"
    shutil.copytree(
        patterns_dir,
        tree,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    local_forbid = root / "solutions" / ".forbid.toml"
    extra = (
        tuple(tomllib.loads(local_forbid.read_text(encoding="utf-8")).get("forbid", ()))
        if local_forbid.is_file()
        else ()
    )
    forbid = (*release.DEFAULT_FORBID, *extra)
    assert release.scan_forbidden(tree, forbid) == []
    (tree / "leak.py").write_text('LEAK = "/Users/alice/secret"\n', encoding="utf-8")
    assert release.scan_forbidden(tree, forbid) != []
