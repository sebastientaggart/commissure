"""Domain-neutral US federal legal citation and date primitives.

Provides compiled regular expressions and canonicalizers for standard federal authorities
(Code of Federal Regulations, United States Code, Public Laws, Federal Register notices,
Statutes at Large, and federal civil dockets) along with prose and numeric date parsers.
Agency- or corpus-specific issuance series stay inside their solution recipes.
"""

from __future__ import annotations

import re
from datetime import date

# --- US federal legal citation regexes -------------------------------------------------------

CFR_RE: re.Pattern[str] = re.compile(
    r"\b(\d{1,2})\s*C\.?F\.?R\.?\s*(?:§+\s*)?(?:[Pp]arts?\s+)?(\d{1,4})(?:\.(\d+))?"
)
CFR_SECTION_RE: re.Pattern[str] = re.compile(
    r"\b(\d{1,2})\s*C\.?F\.?R\.?\s*(?:§+\s*)?(\d{1,4})\.(\d+)\b"
)
USC_RE: re.Pattern[str] = re.compile(
    r"\b(\d{1,2})\s*U\.?\s?S\.?\s?C\.?\s*(?:§+\s*)?(\d+[a-z]?(?:\s?[-–—]\s?\d+[a-z]?)?)\b"
)
PUBLIC_LAW_RE: re.Pattern[str] = re.compile(
    r"\b(?:Pub(?:lic)?\.?\s*L(?:aw)?\.?|P\.\s?L\.)\s*(?:\((?:P\.\s?L\.|Pub\.\s?L\.)\)\s*)?"
    r"(\d{2,3})\s?[-–—]\s?(\d{1,3})\b",
    re.I,
)
FED_REG_RE: re.Pattern[str] = re.compile(
    r"\b(\d{2,3})\s*(?:FR|Fed\.\s?Reg\.)\s*(\d{1,3}(?:,\d{3})+|\d{3,6})\b"
)
STAT_RE: re.Pattern[str] = re.compile(
    r"\b(\d{1,4})\s*STAT\.\s*(\d{1,3}(?:,\d{3})+|\d{1,5})\b",
    re.I,
)
DOCKET_RE: re.Pattern[str] = re.compile(r"\b(?:Case\s+)?No\.\s*(\d:\d{2}-cv-\d+)\b")
COURT_CASE_RE: re.Pattern[str] = re.compile(
    r"\b[A-Z][A-Za-z.]+\s+v\.\s+[^,;]{3,80}?,\s*(?:Case\s+)?No\.\s*(\d:\d{2}-cv-\d+)\b"
)


# --- Canonicalizers --------------------------------------------------------------------------


def canonical_cfr(m: re.Match[str]) -> str:
    """Normalize a ``CFR_RE`` or ``CFR_SECTION_RE`` match (e.g. ``45 CFR 1355.22``)."""
    section = m.group(3) if m.lastindex and m.lastindex >= 3 else None
    return f"{m.group(1)} CFR {m.group(2)}" + (f".{section}" if section else "")


def canonical_usc(m: re.Match[str]) -> str:
    """Normalize a ``USC_RE`` match (e.g. ``42 U.S.C. 671`` or ``42 U.S.C. 670-679b``)."""
    sec = re.sub(r"\s", "", m.group(2)).replace("–", "-").replace("—", "-")
    return f"{m.group(1)} U.S.C. {sec}"


def canonical_public_law(m: re.Match[str]) -> str:
    """Normalize a ``PUBLIC_LAW_RE`` match (e.g. ``P.L. 115-123``)."""
    return f"P.L. {m.group(1)}-{m.group(2)}"


def canonical_fed_reg(m: re.Match[str]) -> str:
    """Normalize a ``FED_REG_RE`` match (e.g. ``89 FR 67720``)."""
    return f"{m.group(1)} FR {m.group(2).replace(',', '')}"


def canonical_stat(m: re.Match[str]) -> str:
    """Normalize a ``STAT_RE`` match (e.g. ``121 Stat. 1363``)."""
    return f"{m.group(1)} Stat. {m.group(2).replace(',', '')}"


def canonical_docket(m: re.Match[str]) -> str:
    """Normalize a ``DOCKET_RE`` or ``COURT_CASE_RE`` match (e.g. ``6:24-cv-348``)."""
    return m.group(1)


def canonical_citation(kind: str, m: re.Match[str]) -> str:
    """Dispatch a citation regex match to its canonical string by ``kind``."""
    if kind == "cfr":
        return canonical_cfr(m)
    if kind == "usc":
        return canonical_usc(m)
    if kind == "public_law":
        return canonical_public_law(m)
    if kind == "fed_reg":
        return canonical_fed_reg(m)
    if kind == "stat":
        return canonical_stat(m)
    if kind in ("court_case", "docket"):
        return canonical_docket(m)
    raise ValueError(f"unknown citation kind: {kind!r}")


canonical = canonical_citation

FEDERAL_CITATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cfr", CFR_RE),
    ("usc", USC_RE),
    ("public_law", PUBLIC_LAW_RE),
    ("fed_reg", FED_REG_RE),
    ("stat", STAT_RE),
    ("court_case", DOCKET_RE),
)


# --- Date regexes and parsers ----------------------------------------------------------------

MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip

PROSE_DATE_RE: re.Pattern[str] = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r",?\s+(\d{4})\b",
    re.I,
)
LOOSE_DATE_RE: re.Pattern[str] = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\D{0,30}?(\d{4})\b",
    re.I,
)
NUMERIC_DATE_RE: re.Pattern[str] = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})\b")


def parse_prose_date(raw: str, *, loose: bool = False) -> date | None:
    """Parse a prose date (``March 3, 2024``, ``Sept. 1st, 2023``) from ``raw``.

    When ``loose=True``, falls back to ``LOOSE_DATE_RE`` if OCR noise sits between the day
    and four-digit year. Returns ``None`` when no match or when the calendar date is invalid.
    """
    m = PROSE_DATE_RE.search(raw)
    if m is None and loose:
        m = LOOSE_DATE_RE.search(raw)
    if m is None:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    except ValueError:
        return None


def parse_numeric_date(raw: str, *, pivot_year: int | None = None) -> date | None:
    """Parse a US numeric date (``M/D/YYYY``, ``M-D-YY``) from ``raw``.

    Two-digit years map to ``2000 + year`` by default. When ``pivot_year`` is set (e.g. ``50``),
    two-digit years ``>= pivot_year`` map to ``1900 + year`` and ``< pivot_year`` to
    ``2000 + year``. Returns ``None`` when no match or when the calendar date is invalid.
    """
    m = NUMERIC_DATE_RE.search(raw)
    if m is None:
        return None
    year = int(m.group(3))
    if year < 100:
        if pivot_year is not None and year >= pivot_year:
            year += 1900
        else:
            year += 2000
    try:
        return date(year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def parse_date(
    raw: str,
    *,
    loose: bool = False,
    pivot_year: int | None = None,
) -> date | None:
    """Parse the first prose or numeric date in ``raw``; return ``None`` if absent or invalid."""
    if PROSE_DATE_RE.search(raw) is not None:
        return parse_prose_date(raw)
    numeric = parse_numeric_date(raw, pivot_year=pivot_year)
    if numeric is not None or NUMERIC_DATE_RE.search(raw) is not None:
        return numeric
    if loose:
        return parse_prose_date(raw, loose=True)
    return None
