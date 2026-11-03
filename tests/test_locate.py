"""locate_offset: half-open spans, misses, sheet and heading paths, OCR flags."""

from __future__ import annotations

import json
from pathlib import Path

from commissure.patterns import locate_offset

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "block_spans" / "two_pages.json"


def _two_pages() -> tuple[str, dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    pages = data["pages"]
    sep = data["separator"]
    assert sep == "\n\n"
    # Neutral characters only; lengths match the transcribed spans.
    p1 = "a" * pages[0]["end"]
    p2 = "b" * (pages[1]["end"] - pages[1]["start"])
    text = p1 + sep + p2
    assert len(text) == pages[1]["end"]
    structure = {"blocks": pages}
    return text, structure


def test_half_open_edges_and_page_from_fixture() -> None:
    text, structure = _two_pages()
    assert locate_offset(text, structure, 0).page == 1
    assert locate_offset(text, structure, 4031).page == 1
    assert locate_offset(text, structure, 4031).kind == "page"
    assert locate_offset(text, structure, 4034).page == 2
    assert locate_offset(text, structure, 7033).page == 2


def test_gap_and_past_end_are_none_not_clamped() -> None:
    text, structure = _two_pages()
    # Separator between pages: Pages.at would clamp to page 1; the helper must miss.
    assert locate_offset(text, structure, 4032) is None
    assert locate_offset(text, structure, 4033) is None
    assert locate_offset(text, structure, 7034) is None
    assert locate_offset(text, structure, -1) is None
    assert locate_offset(text, structure, len(text)) is None
    assert locate_offset(text, structure, len(text) + 10) is None


def test_zero_width_block_contains_nothing() -> None:
    text = "hello"
    structure = {
        "blocks": [
            {"kind": "page", "start": 0, "end": 0, "page": 1, "status": "unreadable"},
            {"kind": "page", "start": 0, "end": 5, "page": 2, "status": "readable"},
        ]
    }
    hit = locate_offset(text, structure, 0)
    assert hit is not None and hit.page == 2


def test_narrowest_then_later_on_overlap() -> None:
    text = "abcdefghij"
    structure = {
        "blocks": [
            {"kind": "page", "start": 0, "end": 10, "page": 1},
            {"kind": "paragraph", "start": 2, "end": 5, "heading_path": ["A"]},
            {"kind": "paragraph", "start": 2, "end": 5, "heading_path": ["B"]},
        ]
    }
    hit = locate_offset(text, structure, 3)
    assert hit is not None
    assert hit.kind == "paragraph"
    assert hit.heading_path == ("B",)
    assert hit.page is None


def test_missing_end_is_not_a_candidate() -> None:
    text = "abcd"
    structure = {
        "blocks": [
            {"kind": "page", "start": 0, "page": 1},
            {"kind": "page", "start": 0, "end": 4, "page": 2},
        ]
    }
    assert locate_offset(text, structure, 1).page == 2


def test_sheet_name_and_heading_path_without_page() -> None:
    text = "sheet body\n\npara under heading"
    structure = {
        "blocks": [
            {"kind": "sheet", "start": 0, "end": 10, "name": "Budget", "table": 0},
            {
                "kind": "paragraph",
                "start": 12,
                "end": 30,
                "heading_path": ["Section I", "Costs"],
            },
        ]
    }
    sheet = locate_offset(text, structure, 0)
    assert sheet is not None
    assert sheet.sheet == "Budget" and sheet.page is None and sheet.kind == "sheet"
    para = locate_offset(text, structure, 12)
    assert para is not None
    assert para.page is None
    assert para.heading_path == ("Section I", "Costs")
    assert para.sheet is None


def test_ocr_flag_and_confidence_from_fixture() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    block = data["ocr_page"]
    text = "x" * block["end"]
    hit = locate_offset(text, {"blocks": [block]}, 0)
    assert hit is not None
    assert hit.via_ocr is True
    assert hit.confidence == 85.0
    assert hit.page == 1


def test_empty_structure_and_none_are_misses() -> None:
    assert locate_offset("abc", None, 0) is None
    assert locate_offset("abc", {}, 0) is None
    assert locate_offset("abc", {"blocks": []}, 1) is None
