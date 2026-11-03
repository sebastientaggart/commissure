"""Keyword and phrase search over current parsed text."""

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from commissure.parse.ocr import ParserUnavailable, tesseract_version
from commissure.runner import RunContext
from commissure.search import SearchFromWorker, SearchUsageError, phrase_query, search
from commissure.workspace import open_workspace

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "probe_corpus"

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

try:
    tesseract_version()
    HAVE_TESSERACT = True
except ParserUnavailable:
    HAVE_TESSERACT = False

needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract binary not installed")


def parse(cli, ws_root, *extra):
    result = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json", *extra)
    return result.exit_code, (json.loads(result.stdout) if result.stdout.strip() else {})


def q(ws_root, sql, *params):
    ws = open_workspace(ws_root)
    try:
        return [dict(r) for r in ws.conn.execute(sql, params)]
    finally:
        ws.close()


@pytest.fixture
def corpus(tmp_path):
    dest = tmp_path / "corpus"
    shutil.copytree(CORPUS, dest)
    return dest


@pytest.fixture
def parsed(cli, ws_root, corpus):
    assert cli.ingest(ws_root, corpus)[0] == 0
    code, report = parse(cli, ws_root)
    assert code == 0, report
    return report


def test_phrase_query_escapes_quotes():
    assert phrase_query('a "b" c') == '"a ""b"" c"'


def test_phrase_hit_maps_to_same_block_as_fts_test(parsed, ws_root):
    ws = open_workspace(ws_root)
    try:
        hits = search(ws.conn, "merged across", limit=20, phrase=True)
    finally:
        ws.close()
    assert [h.locator for h in hits] == ["report.docx"]
    hit = hits[0]
    row = q(
        ws_root,
        "SELECT text, structure FROM current_text WHERE extracted_text_id = ?",
        hit.extracted_text_id,
    )[0]
    assert row["text"][hit.start : hit.end].lower() == "merged across"
    block = next(
        b for b in json.loads(row["structure"])["blocks"] if b["start"] <= hit.start < b["end"]
    )
    assert (block["kind"], block["heading_path"]) == ("table", ["Overview", "Scope"])
    assert hit.parser == "docx"
    assert hit.page is None
    assert hit.via_ocr is False
    assert hit.snippet


@needs_tesseract
def test_ocr_hit_reports_via_ocr_and_confidence(cli, ws_root, tmp_path):
    corpus = tmp_path / "ocr_src"
    shutil.copytree(FIXTURES / "ocr_corpus", corpus)
    assert cli.ingest(ws_root, corpus)[0] == 0
    code, report = parse(cli, ws_root)
    # Both files are scans, so the default parse quarantines everything and fails by design.
    assert code == 1 and report["quarantined"] == 2, report
    code, report = parse(cli, ws_root, "--parser", "ocr")
    assert code == 0, report

    ws = open_workspace(ws_root)
    try:
        hits = search(ws.conn, "Personnel", phrase=True)
    finally:
        ws.close()
    assert hits, "expected a hit on typed_letter.pdf"
    hit = hits[0]
    assert hit.locator == "typed_letter.pdf"
    assert hit.parser == "ocr"
    assert hit.via_ocr is True
    assert hit.confidence is not None and hit.confidence >= 40
    assert hit.page is not None
    structure = json.loads(
        q(
            ws_root,
            "SELECT structure FROM current_text WHERE extracted_text_id = ?",
            hit.extracted_text_id,
        )[0]["structure"]
    )
    block = next(b for b in structure["blocks"] if b["start"] <= hit.start < b["end"])
    assert block["page"] == hit.page
    assert block["confidence"] == hit.confidence
    assert "ocr" in block.get("flags", [])
    assert not any(h.locator == "blank_scan.pdf" for h in hits)


def test_tombstoned_item_is_absent(cli, ws_root, corpus, parsed):
    ws = open_workspace(ws_root)
    try:
        before = search(ws.conn, "merged across", phrase=True)
    finally:
        ws.close()
    assert before

    (corpus / "report.docx").unlink()
    assert cli.ingest(ws_root, corpus)[0] == 0

    ws = open_workspace(ws_root)
    try:
        after = search(ws.conn, "merged across", phrase=True)
        fts_still = ws.conn.execute(
            "SELECT count(*) AS n FROM extracted_text_fts"
            " WHERE extracted_text_fts MATCH '\"merged across\"'"
        ).fetchone()["n"]
    finally:
        ws.close()
    assert after == []
    assert fts_still >= 1


def test_other_parser_row_is_absent_unless_asked(cli, ws_root, corpus, parsed):
    code, report = parse(cli, ws_root, "--parser", "pdfium")
    assert code == 0, report
    by_parser = {
        r["parser"]
        for r in q(ws_root, "SELECT parser FROM current_text WHERE locator = 'native.pdf'")
    }
    assert by_parser == {"pdfplumber", "pdfium"}

    ws = open_workspace(ws_root)
    try:
        # A high limit: "committee" repeats across PDF pages and would fill the
        # default 20-hit cap before native.pdf's pdfium row is reached.
        default_hits = search(ws.conn, "committee", phrase=True, limit=200)
        pdfium_hits = search(ws.conn, "committee", parser="pdfium", phrase=True, limit=200)
    finally:
        ws.close()
    native_default = [h for h in default_hits if h.locator == "native.pdf"]
    assert native_default and {h.parser for h in native_default} == {"pdfplumber"}
    assert not any(h.parser == "pdfium" for h in default_hits if h.locator == "native.pdf")
    native_pdfium = [h for h in pdfium_hits if h.locator == "native.pdf"]
    assert native_pdfium and {h.parser for h in native_pdfium} == {"pdfium"}
    assert not any(h.parser == "pdfplumber" for h in pdfium_hits)


def test_phrase_mode_matches_operators_literally(cli, ws_root, tmp_path):
    src = tmp_path / "ops"
    src.mkdir()
    (src / "notes.json").write_text(
        json.dumps({"body": "the route goes north AND south only"}),
        encoding="utf-8",
    )
    assert cli.ingest(ws_root, src)[0] == 0
    assert parse(cli, ws_root)[0] == 0

    ws = open_workspace(ws_root)
    try:
        assert search(ws.conn, "north AND south", phrase=True)
        assert search(ws.conn, "north OR south", phrase=True) == []
        assert search(ws.conn, "north OR south", phrase=False)
        assert search(ws.conn, "north NOT south", phrase=False) == []
    finally:
        ws.close()


def test_empty_query_is_not_match_all(parsed, ws_root, cli):
    assert q(ws_root, "SELECT count(*) AS n FROM current_text")[0]["n"] > 0
    ws = open_workspace(ws_root)
    try:
        with pytest.raises(SearchUsageError):
            search(ws.conn, "")
        with pytest.raises(SearchUsageError):
            search(ws.conn, "   ")
    finally:
        ws.close()
    result = cli("search", "", "--workspace", str(ws_root), "--json")
    assert result.exit_code == 2


def test_search_from_a_worker_raises(parsed, ws_root):
    ws = open_workspace(ws_root)
    try:
        ctx = RunContext(0, "test", {}, workers=2, conn=ws.conn)
        main_hits = ctx.search("merged across")
        assert main_hits

        def call(_):
            return ctx.search("merged across")

        with pytest.raises(SearchFromWorker):
            ctx.map(call, [1, 2])
    finally:
        ws.close()


def test_search_does_not_take_the_workspace_lock(cli, ws_root, corpus, parsed):
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
        result = cli("search", "merged across", "--workspace", str(ws_root), "--json")
        assert result.exit_code == 0, result.stderr
        hits = json.loads(result.stdout)["hits"]
        assert hits and hits[0]["locator"] == "report.docx"
        assert "start" in hits[0] and "end" in hits[0] and "extracted_text_id" in hits[0]
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
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)


def test_cli_json_and_human_table(cli, ws_root, parsed):
    result = cli("search", "merged across", "--workspace", str(ws_root), "--json")
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "phrase"
    assert report["hits"][0]["locator"] == "report.docx"
    human = cli("search", "merged across", "--workspace", str(ws_root))
    assert human.exit_code == 0
    assert "report.docx" in human.stdout
    assert "docx" in human.stdout
