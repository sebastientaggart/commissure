"""`commissure probe` against tests/fixtures/probe_corpus (see make_probe_corpus.py)."""

import json
import shutil
from pathlib import Path

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from pdfminer.pdftypes import PDFStream
from pdfminer.psparser import LIT

from commissure.probe.office import probe_docx
from commissure.probe.pdf import (
    MIN_DECODABLE,
    PageSignals,
    _clipped_area,
    _page_fonts,
    broken_fonts,
    font_info,
    is_decodable,
    judge,
)
from commissure.probe.tabular import detect_delimiter, probe_csv

CORPUS = Path(__file__).parent / "fixtures" / "probe_corpus"


@pytest.fixture(scope="module")
def report():
    from typer.testing import CliRunner

    from commissure.cli import app

    result = CliRunner().invoke(app, ["probe", str(CORPUS), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    return data, {f["locator"]: f for f in data["files"]}


def _pages(pdf):
    return [(p["verdict"], p["reasons"], p["flags"]) for p in pdf["page_details"]]


def test_counts_by_extension_and_summary(report):
    data, _ = report
    assert data["by_extension"] == {".csv": 2, ".docx": 1, ".pdf": 5, ".txt": 1, ".xlsx": 1}
    assert data["summary"] == {
        "files": 10, "ignored": 0, "inspected": 8, "not_inspected": 1, "errors": 1,
        "pdf_pages": 15, "readable_pages": 7, "unreadable_pages": 7, "blank_pages": 1,
    }  # fmt: skip


def test_native_pdf_is_never_labeled_scanned(report):
    # Page 2 has one word, page 3 is mostly photo, page 4 sits on a full-page image.
    pdf = report[1]["native.pdf"]["pdf"]
    assert "gate_version" not in pdf
    assert (pdf["pages"], pdf["readable"], pdf["unreadable"]) == (4, 4, 0)
    assert _pages(pdf) == [
        ("readable", [], []),
        ("readable", [], []),
        ("readable", [], []),
        ("readable", [], ["full_page_image_under_text"]),
    ]


def test_scanned_pdf_pages_are_flagged(report):
    pdf = report[1]["scanned.pdf"]["pdf"]
    assert _pages(pdf) == [("unreadable", ["image_only"], [])] * 2


def test_mixed_pdf_covers_every_gate_outcome(report):
    pdf = report[1]["mixed.pdf"]["pdf"]
    assert _pages(pdf) == [
        ("readable", [], []),
        ("unreadable", ["image_dominant"], []),
        ("unreadable", ["undecodable_glyphs", "font_without_unicode_map"], []),
        ("readable", [], ["full_page_image_under_text"]),
        ("blank", [], []),
        ("unreadable", ["no_text_layer"], []),
    ]
    fonts = {f["name"]: f["unicode_mapped"] for f in pdf["fonts"]}
    assert fonts == {"GlyphOnlySans": False, "Helvetica": True}
    assert pdf["page_details"][2]["unmapped_fonts"] == ["GlyphOnlySans"]


def test_type3_pdf_covers_encoded_and_unmapped_fonts(report):
    # Page 1 maps codes to AGL glyph names; page 2 has no /Encoding or /ToUnicode;
    # page 3 has /Encoding with custom /gN glyph names: they fail glyph decodability, and
    # names outside the Adobe Glyph List mean the encoding implies no characters either.
    pdf = report[1]["type3.pdf"]["pdf"]
    assert _pages(pdf) == [
        ("readable", [], []),
        ("unreadable", ["undecodable_glyphs", "font_without_unicode_map"], []),
        ("unreadable", ["undecodable_glyphs", "font_without_unicode_map"], []),
    ]
    assert pdf["page_details"][1]["unmapped_fonts"] == ["unknown"]
    assert [f["subtype"] for f in pdf["fonts"]] == ["Type3"]


def test_docx_topology(report):
    d = report[1]["report.docx"]["docx"]
    assert d["headings"] == [
        {"level": 1, "text": "Overview"},
        {"level": 2, "text": "Scope"},
        {"level": 3, "text": "Details"},
        {"level": 1, "text": "Results"},
        {"level": 3, "text": "Skipped a level"},
    ]
    assert d["tables"] == [
        {"index": 0, "rows": 3, "cols": 4, "merged_regions": 1, "under_heading": "Scope"},
        {"index": 1, "rows": 2, "cols": 2, "merged_regions": 0, "under_heading": "Skipped a level"},
    ]
    assert d["flags"] == {
        "heading_level_skips": [{"from": 1, "to": 3, "heading": "Skipped a level"}],
        "possible_unstyled_headings": ["Looks Like A Heading"],
    }


def test_xlsx_two_sheets(report):
    data, notes = report[1]["workbook.xlsx"]["xlsx"]["sheets"]
    assert (data["name"], notes["name"]) == ("Data", "Notes")
    cols = {c["header"]: (c["types"], c["null_rate"]) for c in data["columns"]}
    assert cols == {
        "id": ({"number": 4}, 0.0),
        "name": ({"string": 3}, 0.25),
        "amount": ({"number": 3}, 0.25),
        "date": ({"date": 3}, 0.25),
        "double": ({"formula": 4}, 0.0),  # formulas are not nulls
    }
    assert data["flags"] == ["has_formulas"]
    assert data["sample_rows"][0] == [1, "alpha", 10.5, "2026-01-05T00:00:00", "=C2*2"]
    assert len(data["sample_rows"]) == 3
    assert notes["flags"] == ["possible_title_row"]
    assert (notes["header_row"], notes["data_rows"], notes["empty_rows"]) == (1, 3, 1)


def test_csv_schema(report):
    c = report[1]["table.csv"]["csv"]
    assert (c["encoding"], c["delimiter"]) == ("utf-8-sig", ";")
    assert (c["ragged_rows"], c["ragged_row_numbers_first_20"]) == (1, [4])
    assert [(col["header"], col["types"], col["nulls"]) for col in c["columns"]] == [
        ("code", {"string": 4}, 0),
        ("label", {"string": 3}, 1),
        ("qty", {"number": 2}, 2),
    ]
    assert c["flags"] == ["ragged_rows"]


def test_csv_with_bom_and_unescaped_quote_is_flagged_ragged(report):
    # Row 3 has unescaped inner quotes around a comma-bearing name, splitting into 5 fields.
    c = report[1]["quoted_bom.csv"]["csv"]
    assert (c["encoding"], c["delimiter"]) == ("utf-8-sig", ",")
    assert (c["ragged_rows"], c["ragged_row_numbers_first_20"]) == (1, [3])
    assert c["flags"] == ["ragged_rows"]
    assert c["columns"][4] == {
        "index": 5,
        "letter": "E",
        "header": None,
        "types": {"number": 1},
        "nulls": 3,
        "null_rate": 0.75,
    }


def test_bad_and_unknown_files_are_reported_not_fatal(report):
    files = report[1]
    assert files["broken.pdf"]["status"] == "error"
    assert files["broken.pdf"]["error"]
    assert files["notes.txt"]["status"] == "not_inspected"


# --- gate and helpers on constructed inputs ----------------------------------------


def _signals(**kw):
    base = dict(chars=1000, undecodable_chars=0, broken_font_chars=0, unmapped_font_chars=0,
                image_coverage=0.0,
                text_coverage=0.2, vector_objects=0)  # fmt: skip
    return PageSignals(**{**base, **kw})


def test_gate_decodability_boundary():
    at = round(1000 * (1 - MIN_DECODABLE))
    bad = dict(undecodable_chars=at + 1)
    assert judge(_signals(broken_font_chars=at, **bad)).verdict == "readable"
    assert judge(_signals(broken_font_chars=at + 1, **bad)).reasons == ["undecodable_glyphs"]


def test_gate_flags_unmapped_glyphs_in_working_fonts():
    # One unmapped glyph id (a space) repeated: many bad glyphs, no broken font.
    verdict = judge(_signals(chars=107, undecodable_chars=9, text_coverage=0.09))
    assert (verdict.verdict, verdict.flags) == ("readable", ["unmapped_glyphs"])


def test_gate_separates_stamp_from_ocr_layer_over_a_scan():
    assert judge(_signals(chars=9, image_coverage=1.0, text_coverage=0.001)).reasons == [
        "image_dominant"
    ]
    assert judge(_signals(image_coverage=1.0, text_coverage=0.2)).verdict == "readable"


def test_gate_does_not_judge_by_text_amount():
    assert judge(_signals(chars=3, text_coverage=0.0005)).verdict == "readable"


def _chars(font, texts):
    return [{"fontname": font, "text": t} for t in texts]


def test_broken_fonts_counts_distinct_glyphs_not_occurrences():
    words = list("Department of Health and Human Services 2026")
    space_missing = _chars("A", [t if t != " " else "(cid:1)" for t in words])
    assert broken_fonts(space_missing) == set()
    garbled = _chars("B", [f"(cid:{i})" for i in range(30)] + ["a"])
    assert broken_fonts(garbled) == {"B"}
    # Exactly half of the distinct glyphs bad is not "most": not broken.
    half = _chars("C", ["a", "b", "(cid:1)", "(cid:2)"])
    assert broken_fonts(half) == set()
    assert broken_fonts(half + _chars("C", ["(cid:3)"])) == {"C"}
    assert broken_fonts(space_missing + garbled) == {"B"}
    # One unmapped glyph repeated far more often than the good ones: still one id.
    repeated = _chars("D", ["(cid:1)"] * 20 + ["a", "b", "c"])
    assert broken_fonts(repeated) == set()


def test_is_decodable():
    assert is_decodable("a") and is_decodable("fi") and is_decodable(" ")
    for bad in ("(cid:12)", "\ufffd", "", "\x07"):
        assert not is_decodable(bad), bad


@pytest.mark.parametrize(
    "font, mapped",
    [
        ({"Subtype": LIT("Type1"), "BaseFont": LIT("Helvetica")}, True),
        ({"Subtype": LIT("TrueType"), "BaseFont": LIT("ABCDEF+Wingdings"),
          "FontDescriptor": {"Flags": 4}}, False),
        ({"Subtype": LIT("TrueType"), "BaseFont": LIT("ABCDEF+Arial"),
          "FontDescriptor": {"Flags": 32}}, True),
        ({"Subtype": LIT("TrueType"), "BaseFont": LIT("Arial"),
          "Encoding": LIT("WinAnsiEncoding"), "FontDescriptor": {"Flags": 4}}, True),
        ({"Subtype": LIT("Type0"), "Encoding": LIT("Identity-H"),
          "DescendantFonts": [{"CIDSystemInfo": {"Ordering": b"Identity"}}]}, False),
        ({"Subtype": LIT("Type0"), "Encoding": LIT("Identity-H"),
          "DescendantFonts": [{"CIDSystemInfo": {"Ordering": b"Japan1"}}]}, True),
        ({"Subtype": LIT("Type0"), "Encoding": LIT("Identity-H"), "ToUnicode": object(),
          "DescendantFonts": [{"CIDSystemInfo": {"Ordering": b"Identity"}}]}, True),
        ({"Subtype": LIT("Type3")}, False),
        ({"Subtype": LIT("Type3"), "Encoding": {"Differences": []}}, True),
        ({"Subtype": LIT("Type3"), "ToUnicode": object()}, True),
    ],
)  # fmt: skip
def test_font_unicode_mapping(font, mapped):
    assert font_info(font)["unicode_mapped"] is mapped


@pytest.mark.parametrize(
    "sample, expected",
    [
        ("a;b;c\n1;2;3\n4;5\n6;7;8\n", ";"),
        ('a;b\n"x, y";2\n"z, w";3\n', ";"),
        ("a,b\tc\n1,2\t3\n", ","),
        ("only\none\ncolumn\n", None),
    ],
)
def test_detect_delimiter(sample, expected):
    assert detect_delimiter(sample) == expected


def test_fonts_inside_form_xobjects_are_found():
    inner = {"Subtype": LIT("Type1"), "BaseFont": LIT("InnerFont")}
    form = PDFStream({"Subtype": LIT("Form"), "Resources": {"Font": {"F9": inner}}}, b"")
    resources = {"Font": {"F1": {"BaseFont": LIT("Outer")}}, "XObject": {"Fm1": form}}
    assert [f["BaseFont"].name for f in _page_fonts(resources)] == ["Outer", "InnerFont"]


def test_image_area_is_clipped_to_the_page():
    off_page = [{"x0": -100, "x1": 100, "top": 0, "bottom": 100}]
    assert _clipped_area(off_page, (0, 0, 612, 792)) == 100 * 100


def test_docx_heading_sources_and_vertical_merges(tmp_path):
    doc = Document()
    styled = doc.add_heading("By outline level", level=2)
    # Strip the outline level from the style: the name "Heading 2" must still count.
    no_outline = doc.add_heading("By style name", level=3)
    ppr = no_outline.style.element.pPr
    ppr.remove(ppr.find(qn("w:outlineLvl")))
    # A custom style that only carries an outline level.
    custom = doc.styles.add_style("Section Title", WD_STYLE_TYPE.PARAGRAPH)
    lvl = OxmlElement("w:outlineLvl")
    lvl.set(qn("w:val"), "0")
    custom.element.get_or_add_pPr().append(lvl)
    doc.add_paragraph("By custom style", style="Section Title")
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 1).merge(table.cell(2, 1))  # one vertical region over three rows
    path = tmp_path / "h.docx"
    doc.save(path)
    assert styled.style.name == "Heading 2"

    d = probe_docx(path)
    assert d["headings"] == [
        {"level": 2, "text": "By outline level"},
        {"level": 3, "text": "By style name"},
        {"level": 1, "text": "By custom style"},
    ]
    assert d["tables"][0]["merged_regions"] == 1


def test_csv_that_is_not_utf8_is_decoded_as_cp1252_and_flagged(tmp_path):
    path = tmp_path / "legacy.csv"
    path.write_bytes("name,city\nRen\u00e9,Montr\u00e9al\n".encode("cp1252"))
    c = probe_csv(path, sample_rows=1)
    assert c["encoding"] == "cp1252"
    assert c["sample_rows"] == [["Ren\u00e9", "Montr\u00e9al"]]
    assert "not_utf8_decoded_as_cp1252" in c["flags"]


# --- CLI behaviour ------------------------------------------------------------------


def test_probe_exit_codes(cli, tmp_path):
    assert cli("probe", str(tmp_path / "missing")).exit_code == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli("probe", str(empty)).exit_code == 1
    only_broken = tmp_path / "broken"
    only_broken.mkdir()
    shutil.copy(CORPUS / "broken.pdf", only_broken)
    assert cli("probe", str(only_broken), "--json").exit_code == 1
    assert cli("probe", str(CORPUS / "scanned.pdf")).exit_code == 0


def test_single_file_and_sample_rows(cli):
    result = cli("probe", str(CORPUS / "workbook.xlsx"), "--json", "--sample-rows", "0")
    data = json.loads(result.stdout)
    assert [f["locator"] for f in data["files"]] == ["workbook.xlsx"]
    assert all(s["sample_rows"] == [] for s in data["files"][0]["xlsx"]["sheets"])


def test_human_output_escapes_file_names(cli, tmp_path):
    (tmp_path / "[bold]odd.txt").write_text("x")
    result = cli("probe", str(tmp_path))
    assert result.exit_code == 0
    assert "[bold]odd.txt" in result.stdout


def test_probe_writes_nothing(cli, tmp_path):
    corpus = tmp_path / "corpus"
    shutil.copytree(CORPUS, corpus)

    def snapshot():
        return {
            p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in tmp_path.rglob("*") if p.is_file()
        }  # fmt: skip

    before = snapshot()
    assert cli("probe", str(corpus), "--json").exit_code == 0
    assert cli("probe", str(corpus)).exit_code == 0
    assert snapshot() == before
