"""The `ocr` parser: Tesseract reads the pages the gate rejects.

Through the real CLI and the production schema. Fixtures in tests/fixtures/ocr_corpus/
are image-only PDFs written by make_ocr_corpus.py: typed_letter.pdf (two pages of known
text) and blank_scan.pdf (noise, no writing: the page must stay unreadable).
"""

import json
import shutil
from pathlib import Path

import pytest

from commissure.parse import PARSERS
from commissure.parse.ocr import ParserUnavailable, tesseract_version
from commissure.workspace import open_workspace

FIXTURES = Path(__file__).parent / "fixtures"

try:
    tesseract_version()
    HAVE_TESSERACT = True
except ParserUnavailable:
    HAVE_TESSERACT = False

needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract binary not installed")


def q(ws_root, sql, *params):
    ws = open_workspace(ws_root)
    try:
        return [dict(r) for r in ws.conn.execute(sql, params)]
    finally:
        ws.close()


def parse(cli, ws_root, *extra):
    result = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json", *extra)
    return result.exit_code, (json.loads(result.stdout) if result.stdout.strip() else {}), result


@pytest.fixture
def corpus(tmp_path):
    dest = tmp_path / "src"
    shutil.copytree(FIXTURES / "ocr_corpus", dest)
    (dest / "note.json").write_text(json.dumps({"name": "note", "amount": 1}))
    return dest


def _text_rows(ws_root):
    return {
        (r["locator"], r["parser"]): r
        for r in q(
            ws_root,
            "SELECT i.locator, ct.parser, ct.text, ct.structure FROM current_text ct"
            " JOIN item_versions v ON v.id = ct.item_version_id JOIN items i ON i.id = v.item_id",
        )
    }


@needs_tesseract
def test_ocr_recognises_image_only_pages_and_refuses_noise(cli, ws_root, corpus):
    assert cli.ingest(ws_root, corpus)[0] == 0
    code, default, _ = parse(cli, ws_root)
    assert code == 0 and default["quarantined"] == 2  # no text layer: pdfplumber finds nothing

    code, report, result = parse(cli, ws_root, "--parser", "ocr")
    assert code == 0, result.output
    assert (report["discovered"], report["warning"], report["quarantined"]) == (2, 1, 1)

    rows = _text_rows(ws_root)
    assert ("typed_letter.pdf", "pdfplumber") not in rows  # quarantined: no row (Identity 5)
    letter = rows[("typed_letter.pdf", "ocr")]
    for word in ("Budget", "Justification", "2025", "Personnel", "Volunteer", "Federal"):
        assert word in letter["text"], word
    structure = json.loads(letter["structure"])
    pages = [b for b in structure["blocks"] if b["kind"] == "page"]
    assert [(b["page"], b["status"], b["flags"]) for b in pages] == [
        (1, "readable", ["ocr"]),
        (2, "readable", ["ocr"]),
    ]
    assert all(b["confidence"] >= 40 for b in pages)
    # Page text spans tile the text: evidence can carry a page number.
    assert letter["text"][pages[0]["start"] : pages[0]["end"]].startswith("Budget")

    outcomes = {
        r["locator"]: r
        for r in q(
            ws_root,
            "SELECT i.locator, co.outcome, co.reason_code, co.diagnostics FROM current_outcomes co"
            " JOIN items i ON 'item:' || i.id = co.subject_key WHERE co.stage = 'parse.ocr'",
        )
    }
    assert outcomes["typed_letter.pdf"]["outcome"] == "warning"
    assert outcomes["typed_letter.pdf"]["reason_code"] == "ocr_pages"
    diag = {d["code"]: d for d in json.loads(outcomes["typed_letter.pdf"]["diagnostics"])}
    assert diag["ocr_pages"]["detail"]["pages"] == [1, 2]

    # The check can fail: noise stays unreadable, with the OCR reason added to image_only.
    assert outcomes["blank_scan.pdf"]["outcome"] == "quarantined"
    assert outcomes["blank_scan.pdf"]["reason_code"].startswith("no_readable_text")
    diag = {d["code"]: d for d in json.loads(outcomes["blank_scan.pdf"]["diagnostics"])}
    reasons = diag["unreadable_pages"]["detail"]["reasons"]["1"]
    assert reasons[0] == "image_only" and reasons[1].startswith("ocr_")
    assert ("blank_scan.pdf", "ocr") not in rows


@needs_tesseract
def test_stage_prefers_ocr_text_when_a_row_exists(cli, ws_root, corpus, tmp_path):
    """`text=("ocr", "default")`: an item uses its ocr row when there is one, its default
    parser's row otherwise, and `item.parser` says which."""
    recipe = tmp_path / "r"
    recipe.mkdir()
    (recipe / "recipe.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n"
        "class W(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    parser: str\n"
        "    words: int\n"
        "def words(ctx, item):\n"
        "    return W(parser=item.parser, words=len(item.text.split()))\n"
        "STAGES = [Stage('words', words, schema=W, text=('ocr', 'default'))]\n"
    )

    def run_words():
        result = cli(
            "run", "--step", "words", "--workspace", str(ws_root), "--recipe", str(recipe),
            "--json",
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        return {
            r["locator"]: (r["outcome"], r["reason_code"], r["payload"])
            for r in q(
                ws_root,
                "SELECT i.locator, co.outcome, co.reason_code, rec.payload FROM current_outcomes co"
                " JOIN items i ON 'item:' || i.id = co.subject_key"
                " LEFT JOIN records rec ON rec.id = co.record_id WHERE co.stage = 'r.words'",
            )
        }

    assert cli.ingest(ws_root, corpus)[0] == 0
    assert parse(cli, ws_root)[0] == 0
    before = run_words()
    assert before["note.json"][0] == "valid"
    assert json.loads(before["note.json"][2])["parser"] == "json"
    assert before["typed_letter.pdf"][:2] == ("skipped", "no_text")
    assert before["blank_scan.pdf"][:2] == ("skipped", "no_text")

    assert parse(cli, ws_root, "--parser", "ocr")[0] == 0
    after = run_words()
    assert after["typed_letter.pdf"][0] == "valid"
    payload = json.loads(after["typed_letter.pdf"][2])
    assert payload["parser"] == "ocr" and payload["words"] > 30
    assert after["blank_scan.pdf"][:2] == ("skipped", "no_text")
    assert json.loads(after["note.json"][2])["parser"] == "json"


def test_ocr_skips_born_digital_pdf_and_stage_uses_default_row(
    cli, ws_root, corpus, tmp_path, monkeypatch
):
    """A born-digital PDF with no unreadable pages is skipped by `parse --parser ocr`
    (no duplicate `ocr` row in `extracted_text`), and `text=('ocr', 'default')` uses its
    `pdfplumber` row."""
    if not HAVE_TESSERACT:
        import pytesseract

        monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.0.0")

        def fake_ocr(image, **_kwargs):
            # Noise image in blank_scan.pdf has mid-gray pixels; typed_letter has dark text.
            extrema = image.getextrema()
            has_text = extrema[0] < 50
            word = "Budget Justification 2025 Personnel" if has_text else ""
            return {
                "text": [word],
                "conf": [90.0 if has_text else -1.0],
                "block_num": [1],
                "par_num": [1],
                "line_num": [1],
            }

        monkeypatch.setattr(pytesseract, "image_to_data", fake_ocr)
    shutil.copy(FIXTURES / "probe_corpus" / "native.pdf", corpus / "native.pdf")
    recipe = tmp_path / "r"
    recipe.mkdir()
    (recipe / "recipe.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n"
        "class W(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    parser: str\n"
        "STAGES = [Stage('words', lambda ctx, item: W(parser=item.parser),\n"
        "                schema=W, text=('ocr', 'default'))]\n"
    )
    assert cli.ingest(ws_root, corpus)[0] == 0
    assert parse(cli, ws_root)[0] == 0
    code, report, _ = parse(cli, ws_root, "--parser", "ocr")
    assert code == 0
    assert (report["discovered"], report["warning"], report["quarantined"], report["skipped"]) == (
        3,
        1,
        1,
        1,
    )
    rows = _text_rows(ws_root)
    assert ("native.pdf", "pdfplumber") in rows
    assert ("native.pdf", "ocr") not in rows
    res = cli(
        "run", "--step", "words", "--workspace", str(ws_root), "--recipe", str(recipe), "--json"
    )
    assert res.exit_code == 0, res.output
    by_loc = {
        r["locator"]: json.loads(r["payload"])["parser"]
        for r in q(
            ws_root,
            "SELECT i.locator, rec.payload FROM current_records rec"
            " JOIN items i ON 'item:' || i.id = rec.subject_key WHERE rec.stage = 'r.words'",
        )
    }
    assert by_loc["native.pdf"] == "pdfplumber"
    assert by_loc["typed_letter.pdf"] == "ocr"


def test_ocr_parser_without_tesseract_is_a_usage_error(cli, ws_root, corpus, monkeypatch):
    import pytesseract

    def missing():
        raise RuntimeError("tesseract is not installed or it's not in your PATH")

    monkeypatch.setattr(pytesseract, "get_tesseract_version", missing)
    with pytest.raises(ParserUnavailable, match="tesseract binary"):
        PARSERS["ocr"].check()
    assert cli.ingest(ws_root, corpus)[0] == 0
    code, _, result = parse(cli, ws_root, "--parser", "ocr")
    assert code == 2
    assert "tesseract" in result.stderr
    assert q(ws_root, "SELECT count(*) AS n FROM outcomes WHERE stage = 'parse.ocr'")[0]["n"] == 0


def test_default_parse_never_runs_ocr(cli, ws_root, corpus):
    """Without --parser the default parsers run; the ocr parser's version is never computed,
    so a machine without tesseract parses everything else normally."""
    assert cli.ingest(ws_root, corpus)[0] == 0
    assert parse(cli, ws_root)[0] == 0
    stages = {r["stage"] for r in q(ws_root, "SELECT DISTINCT stage FROM outcomes")}
    assert "parse.ocr" not in stages
    assert {"parse.pdfplumber", "parse.json"} <= stages


@needs_tesseract
def test_ocr_version_names_the_binary_and_language():
    assert "tesseract=" in PARSERS["ocr"].version
    assert "lang=eng" in PARSERS["ocr"].version


@needs_tesseract
def test_upside_down_page_is_turned_and_recognised():
    """Content drawn upside down inside the page reads as noise until OSD turns it. The
    fixture is typed_letter page 1 rotated 180 degrees (make_ocr_corpus.py)."""
    from commissure.parse.ocr import parse_ocr

    doc = parse_ocr(FIXTURES / "ocr_rotated" / "upside_down_letter.pdf")
    (page,) = [b for b in doc.blocks if b.kind == "page"]
    assert page.status == "readable", page.reasons
    assert page.flags == ["ocr", "ocr_rotated_180"]
    assert page.confidence is not None and page.confidence >= 80
    assert "Budget Justification for Fiscal Year 2025" in doc.text


@needs_tesseract
def test_type3_page_with_unnamed_glyphs_is_recognised():
    """A re-distilled PDF whose Type 3 glyph names resolve to nothing: the gate
    rejects both pages (font_without_unicode_map) and OCR reads the header correctly."""
    from commissure.parse.ocr import parse_ocr

    doc = parse_ocr(FIXTURES / "real_samples" / "type3_unnamed.pdf")
    pages = [b for b in doc.blocks if b.kind == "page"]
    assert [(b.status, b.flags[:1]) for b in pages] == [("readable", ["ocr"])] * 2
    assert "MEMORANDUM 2024-10" in doc.text
    assert "December 13, 2024" in doc.text
    assert "(cid:" not in doc.text
