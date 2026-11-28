"""Unit tests for commissure/patterns/text.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from commissure.api import Evidence, Item, Source
from commissure.evidence import resolve
from commissure.patterns import (
    ABBREVIATIONS,
    SYMBOL_RATE_LIMIT,
    SYMBOLS,
    Flat,
    Line,
    lines_of,
    page_lines,
    sentence_bounds,
    symbol_rate,
    trim_line,
)


class _SampleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentence: str
    evidence: Evidence


def _make_item(text: str, structure: dict | None = None) -> Item:
    return Item(
        key="corpus:doc.pdf",
        item_version_id=10,
        locator="doc.pdf",
        role="document",
        bytes_hash="00" * 32,
        size_bytes=len(text),
        path=Path("doc.pdf"),
        source=Source(name="corpus", uri="/tmp/corpus"),
        text=text,
        structure=structure,
        parser="default",
        extracted_text_id=100,
    )


def test_symbol_rate_distinguishes_prose_from_glyph_garbage() -> None:
    assert symbol_rate("") == 0.0
    assert symbol_rate("   \n\t  ") == 0.0
    assert symbol_rate("a!b@ c") == pytest.approx(0.4)

    prose = (
        "This memorandum provides guidance on statutory reporting requirements and "
        "administrative procedures for state and tribal grant recipients."
    )
    contacts = "Region I — Jane Doe (617) 555-0100 Room 402\n" * 5
    garbage = "!\"#$%&'()*+ /0/1/2 [\\]^_{|}~ " * 10

    assert symbol_rate(prose) < SYMBOL_RATE_LIMIT
    assert symbol_rate(contacts) < SYMBOL_RATE_LIMIT
    assert symbol_rate(garbage) > SYMBOL_RATE_LIMIT
    assert "!" in SYMBOLS and "a" not in SYMBOLS


def test_lines_of_maps_offsets_pages_and_ocr_without_clamping() -> None:
    p1_line = "  First page heading  \r\n"
    gap_line = "\n  unmapped gap\n"
    p2_line = "   Scanned second page   \n"
    text = p1_line + gap_line + p2_line

    p1_end = len(p1_line)
    p2_start = len(p1_line) + len(gap_line)
    structure = {
        "blocks": [
            {"kind": "page", "start": 0, "end": p1_end, "page": 1, "status": "readable"},
            {
                "kind": "page",
                "start": p2_start,
                "end": len(text),
                "page": 2,
                "flags": ["ocr"],
                "confidence": 88.5,
            },
        ]
    }

    lines = lines_of(text, structure)
    assert len(lines) == 3
    for ln in lines:
        assert text[ln.offset : ln.offset + len(ln.text)] == ln.text

    assert lines[0] == Line(
        text="First page heading", offset=2, page=1, via_ocr=False, confidence=None
    )
    assert lines[1] == Line(
        text="unmapped gap",
        offset=len(p1_line) + 3,
        page=None,
        via_ocr=False,
        confidence=None,
    )
    assert lines[2] == Line(
        text="Scanned second page",
        offset=p2_start + 3,
        page=2,
        via_ocr=True,
        confidence=88.5,
    )

    trimmed = trim_line(lines[0], len("First "))
    assert trimmed is not None
    assert trimmed.text == "page heading"
    assert text[trimmed.offset : trimmed.offset + len(trimmed.text)] == "page heading"
    assert trim_line(lines[0], len(lines[0].text)) is None

    assert page_lines(lines, 1) == [lines[0]]
    assert page_lines(lines, 2) == [lines[0], lines[2]]
    no_pages = lines_of("Alpha\nBeta\n")
    assert page_lines(no_pages, 1) == no_pages


def test_sentence_bounds_respects_abbreviations_initials_and_quotes() -> None:
    assert {"u.s", "p.l", "sec", "secs", "jan", "v"} <= ABBREVIATIONS
    text = (
        "First sentence ends here. "
        "Under 42 U.S.C. sec. 671 and Pub. L. 115-123, see J. R. Smith v. Doe Inc."
        " on Jan. 5, 2024. "
        '"Quoted third sentence!"'
    )
    target = "Pub. L. 115-123"
    start = text.index(target)
    end = start + len(target)
    s, e = sentence_bounds(text, start, end)
    extracted = text[s:e].strip()
    assert extracted == (
        "Under 42 U.S.C. sec. 671 and Pub. L. 115-123, see J. R. Smith v. Doe Inc. on Jan. 5, 2024."
    )

    q_start = text.index("Quoted")
    qs, qe = sentence_bounds(text, q_start, q_start + 6)
    assert text[qs:qe].strip() == '"Quoted third sentence!"'

    with pytest.raises(ValueError, match="outside text"):
        sentence_bounds(text, -1, 5)
    with pytest.raises(ValueError, match="outside text"):
        sentence_bounds(text, 10, 5)


def test_flat_joins_hyphen_wraps_and_maps_spans_to_item_evidence() -> None:
    raw = (
        "  Introductory note.  \n"
        "  This instruction applies P.L. 115-\n"
        "    123 and 45 CFR\n"
        "    1355.20 to all grants.  \n"
        "  Part II -\n"
        "    Reporting rules follow.  \n"
    )
    structure = {
        "blocks": [
            {
                "kind": "page",
                "start": 0,
                "end": len(raw),
                "page": 3,
                "flags": ["ocr"],
                "confidence": 91.0,
            }
        ]
    }
    item = _make_item(raw, structure)
    lines = lines_of(raw, structure)
    flat = Flat.of(lines, item=item)

    # Hyphen at end of line joins without space; spaced dash " -" keeps its space.
    assert "P.L. 115-123" in flat.text
    assert "45 CFR 1355.20" in flat.text
    assert "Part II - Reporting" in flat.text

    pl_pos = flat.text.index("P.L. 115-123")
    pl_end = pl_pos + len("P.L. 115-123")
    line_at_start = flat.at(pl_pos)
    line_at_end = flat.at(pl_end - 1)
    assert line_at_start is not None and line_at_start.text.endswith("P.L. 115-")
    assert line_at_end is not None and line_at_end.text.startswith("123 and 45 CFR")
    assert flat.at(-1) is None
    assert flat.at(len(flat.text)) is None

    item_start, item_end = flat.item_span(pl_pos, pl_end)
    assert raw[item_start:item_end] == "P.L. 115-\n    123"

    sentence, ev = flat.span_evidence(pl_pos, pl_end)
    assert sentence == "This instruction applies P.L. 115-123 and 45 CFR 1355.20 to all grants."
    assert ev.text == "P.L. 115-\n    123"
    assert ev.kind == "quote"

    # Verify that core's evidence resolver accepts the span and fills page + OCR fields.
    doc, rows = resolve(_SampleRecord(sentence=sentence, evidence=ev), item)
    assert len(rows) == 1
    assert doc["evidence"]["page"] == 3
    assert doc["evidence"]["via_ocr"] is True
    assert doc["evidence"]["confidence"] == 91.0

    with pytest.raises(ValueError, match="outside flattened text"):
        flat.item_span(5, 5)
    with pytest.raises(ValueError, match="needs the Item"):
        Flat.of(lines).span_evidence(pl_pos, pl_end)
