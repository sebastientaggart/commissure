"""`commissure recon` measures a parsed workspace and writes nothing."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from commissure.parse.ocr import ParserUnavailable, tesseract_version
from commissure.recon import recon_workspace
from commissure.workspace import open_workspace

FIXTURES = Path(__file__).parent / "fixtures"
RECON = FIXTURES / "recon_corpus"
OCR = FIXTURES / "ocr_corpus"

try:
    tesseract_version()
    HAVE_TESSERACT = True
except ParserUnavailable:
    HAVE_TESSERACT = False

needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract binary not installed")

HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from commissure.workspace import lock

    with lock(Path(sys.argv[1])):
        print("locked", flush=True)
        sys.stdin.readline()
    """
)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    dest = tmp_path / "src"
    shutil.copytree(RECON, dest)
    scan = dest / "scan"
    scan.mkdir()
    shutil.copy(OCR / "typed_letter.pdf", scan / "typed_letter.pdf")
    return dest


def parse(cli, ws_root, *extra):
    result = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json", *extra)
    return result.exit_code, (json.loads(result.stdout) if result.stdout.strip() else {}), result


def recon(cli, ws_root, *extra):
    result = cli("recon", "--workspace", str(ws_root), "--json", *extra)
    report = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, report, result


def _line(report, text: str):
    return next((row for row in report["repeated_lines"] if row["line"] == text), None)


def _prep(cli, ws_root, corpus):
    assert cli.ingest(ws_root, corpus)[0] == 0
    code, _, result = parse(cli, ws_root)
    assert code == 0, result.output


def test_repeated_line_is_listed_with_its_pages(cli, ws_root, corpus):
    _prep(cli, ws_root, corpus)
    code, report, result = recon(cli, ws_root)
    assert code == 0, result.stderr
    row = _line(report, "REPEATED LINE ON EVERY PAGE")
    assert row is not None, "repeated line omitted from report"
    assert row["files"] == 1
    assert row["pages"] == 3
    locs = {e["locator"]: e for e in row["locators"]}
    assert "alpha/repeat.pdf" in locs
    assert locs["alpha/repeat.pdf"]["pages"] == [1, 2, 3]


def test_identical_text_hash_group_names_both_locators(cli, ws_root, corpus):
    _prep(cli, ws_root, corpus)
    code, report, result = recon(cli, ws_root)
    assert code == 0, result.stderr
    twins = [
        g for g in report["identical"] if set(g["locators"]) >= {"alpha/twin.pdf", "beta/twin.pdf"}
    ]
    assert twins, "identical twin pair omitted from report"
    group = twins[0]
    assert group["parser"] == "pdfplumber"
    assert group["size"] == 2
    assert "alpha/repeat.pdf" not in group["locators"]


@needs_tesseract
def test_ocr_page_is_listed_with_confidence(cli, ws_root, corpus):
    _prep(cli, ws_root, corpus)
    code, before, result = recon(cli, ws_root)
    assert code == 0, result.stderr
    scan_before = [p for p in before["pages"] if p["locator"] == "scan/typed_letter.pdf"]
    assert scan_before, "unreadable scan pages omitted before OCR"
    assert all(p["ocr"] == "not_run" for p in scan_before)
    assert all("text" not in p for p in scan_before)

    code, _, result = parse(cli, ws_root, "--parser", "ocr")
    assert code == 0, result.output
    code, after, result = recon(cli, ws_root)
    assert code == 0, result.stderr
    recognised = [
        p
        for p in after["pages"]
        if p["locator"] == "scan/typed_letter.pdf" and p.get("ocr") == "recognised"
    ]
    assert recognised, "OCR page omitted from report"
    assert all("ocr" in (p.get("flags") or []) for p in recognised)
    assert all(isinstance(p.get("confidence"), (int, float)) for p in recognised)

    items = {i["locator"]: i for i in after["items"]}
    assert items["scan/typed_letter.pdf"]["parser"] == "ocr"


def test_one_bad_file_does_not_drop_the_report(cli, ws_root, corpus):
    _prep(cli, ws_root, corpus)
    code, report, result = recon(cli, ws_root)
    assert code == 0, result.stderr
    err_locs = {e["locator"] for e in report["errors"]}
    assert "gamma/broken.pdf" in err_locs
    items = {i["locator"]: i for i in report["items"]}
    assert items["gamma/broken.pdf"]["status"] == "error"
    assert items["loose.txt"]["status"] == "unparsed"
    assert items["loose.txt"]["parse_reason"] == "no_parser"
    assert _line(report, "REPEATED LINE ON EVERY PAGE") is not None


def test_parsed_sheet_signature_is_the_first_row(cli, ws_root, corpus):
    _prep(cli, ws_root, corpus)
    code, report, result = recon(cli, ws_root)
    assert code == 0, result.stderr
    match = [
        s
        for s in report["table_signatures"]
        if s["first_row"] == ["Alpha", "Beta"]
        and any(
            e["locator"] == "beta/sheet.xlsx" and e["source"] == "parsed_table"
            for e in s["locators"]
        )
    ]
    assert match, "parsed sheet first row omitted"
    assert match[0]["column_count"] == 2


def test_unparsed_sheet_uses_probe_signature(cli, ws_root, tmp_path):
    src = tmp_path / "sheet_only"
    src.mkdir()
    shutil.copy(RECON / "beta" / "sheet.xlsx", src / "sheet.xlsx")
    assert cli.ingest(ws_root, src)[0] == 0
    code, report, result = recon(cli, ws_root)
    assert code == 0, result.stderr
    match = [
        s
        for s in report["table_signatures"]
        if s["first_row"] == ["Alpha", "Beta"]
        and any(
            e["locator"] == "sheet.xlsx" and e["source"] == "probe_sheet" for e in s["locators"]
        )
    ]
    assert match, "probe sheet signature omitted for unparsed xlsx"
    assert any(e.get("assumption") == "first non-empty row" for e in match[0]["locators"])
    items = {i["locator"]: i for i in report["items"]}
    assert items["sheet.xlsx"]["status"] == "unparsed"


@needs_tesseract
def test_selected_parser_matches_ocr_then_default(cli, ws_root, corpus):
    _prep(cli, ws_root, corpus)
    assert parse(cli, ws_root, "--parser", "ocr")[0] == 0
    code, report, result = recon(cli, ws_root)
    assert code == 0, result.stderr
    items = {i["locator"]: i for i in report["items"]}
    assert items["scan/typed_letter.pdf"]["parser"] == "ocr"
    assert items["alpha/twin.pdf"]["parser"] == "pdfplumber"
    for group in report["identical"]:
        assert "scan/typed_letter.pdf" not in group["locators"]


def test_recon_does_not_lock_or_migrate(cli, ws_root, corpus, tmp_path):
    assert cli.ingest(ws_root, corpus)[0] == 0
    db = ws_root / "state.db"
    before_db = db.read_bytes()
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(ws_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        code, report, result = recon(cli, ws_root)
        assert code == 0, result.stderr
        assert report["summary"]["items"] > 0
        refused = cli(
            "run",
            "--step",
            "ingest",
            "--workspace",
            str(ws_root),
            "--source",
            str(corpus),
        )
        assert refused.exit_code == 3
        assert db.read_bytes() == before_db
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)

    root = tmp_path / "old"
    root.mkdir()
    old_db = root / "state.db"
    sqlite3.connect(old_db).close()
    before = old_db.read_bytes()
    result = cli("recon", "--workspace", str(root))
    assert result.exit_code == 2
    assert "schema version 0" in result.stderr
    assert "does not migrate" in result.stderr
    assert old_db.read_bytes() == before


def test_invalid_structure_json_does_not_drop_siblings(cli, ws_root, corpus):
    _prep(cli, ws_root, corpus)
    ws = open_workspace(ws_root)
    try:
        row = ws.conn.execute(
            "SELECT et.id FROM extracted_text et"
            " JOIN item_versions v ON v.id = et.item_version_id"
            " JOIN items i ON i.id = v.item_id"
            " WHERE i.locator = ?",
            ("alpha/twin.pdf",),
        ).fetchone()
        assert row is not None
        # Schema requires json_valid(structure); a JSON array is valid JSON but not
        # the object recon expects, so the item errors and siblings stay.
        ws.conn.execute("UPDATE extracted_text SET structure = ? WHERE id = ?", ("[]", row[0]))
        ws.conn.commit()
    finally:
        ws.close()

    report = recon_workspace(ws_root)
    err_locs = {e["locator"] for e in report["errors"]}
    assert "alpha/twin.pdf" in err_locs
    assert _line(report, "REPEATED LINE ON EVERY PAGE") is not None
    items = {i["locator"]: i for i in report["items"]}
    assert items["alpha/twin.pdf"]["status"] == "error"
    assert items["alpha/repeat.pdf"]["status"] == "summarized"
