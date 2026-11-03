"""`commissure run --step parse` behaviour, through the CLI and the production schema."""

import itertools
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from commissure.parse import PARSERS, Parser
from commissure.parse.pdf import parse_pdfium, parse_pdfplumber
from commissure.parse.tabular import parse_json
from commissure.workspace import DB_NAME, MIGRATIONS, connect, open_workspace, utc_now

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "probe_corpus"
REAL = FIXTURES / "real_samples"


def parse(cli, ws_root, *extra):
    result = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json", *extra)
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload


def q(ws_root, sql, *params):
    ws = open_workspace(ws_root)
    rows = [dict(r) for r in ws.conn.execute(sql, params)]
    ws.close()
    return rows


def count(ws_root, table):
    return q(ws_root, f"SELECT count(*) AS n FROM {table}")[0]["n"]


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


def set_pdf_title(path: Path, title: str) -> None:
    """Append an incremental update that only sets /Info /Title (a metadata-only edit)."""
    data = path.read_bytes()
    root = re.search(rb"/Root (\d+) 0 R", data).group(1).decode()
    size = int(re.findall(rb"/Size (\d+)", data)[-1])
    prev = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    obj = f"{size} 0 obj\n<< /Title ({title}) >>\nendobj\n".encode()
    offset = len(data)
    xref = offset + len(obj)
    tail = (
        f"xref\n{size} 1\n{offset:010d} 00000 n \n"
        f"trailer\n<< /Size {size + 1} /Root {root} 0 R /Info {size} 0 R /Prev {prev} >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(data + obj + tail)


# --- outcomes on the probe corpus folder --------------------------------------------


def test_fixture_folder_outcomes(parsed, ws_root):
    assert {
        k: parsed[k]
        for k in ("status", "discovered", "valid", "warning", "quarantined", "skipped", "unchanged")
    } == {
        "status": "succeeded",
        "discovered": 10,
        "valid": 1,
        "warning": 6,
        "quarantined": 2,
        "skipped": 1,
        "unchanged": 0,
    }
    rows = q(
        ws_root,
        "SELECT i.locator, o.outcome, o.reason_code, o.extracted_text_id"
        " FROM outcomes o JOIN item_versions v ON v.id = o.item_version_id"
        " JOIN items i ON i.id = v.item_id ORDER BY i.locator",
    )
    got = {
        r["locator"]: (r["outcome"], r["reason_code"], r["extracted_text_id"] is not None)
        for r in rows
    }
    assert got == {
        "broken.pdf": ("quarantined", "parse_error", False),
        "mixed.pdf": ("warning", "full_page_image_under_text,unreadable_pages", True),
        "native.pdf": ("warning", "full_page_image_under_text", True),
        "notes.txt": ("skipped", "no_parser", False),
        "quoted_bom.csv": ("warning", "ragged_rows", True),
        "report.docx": ("valid", None, True),
        "scanned.pdf": ("quarantined", "no_readable_text,unreadable_pages", False),
        "table.csv": ("warning", "ragged_rows", True),
        "type3.pdf": ("warning", "unreadable_pages", True),
        "workbook.xlsx": ("warning", "uncached_formulas", True),
    }
    run = q(ws_root, "SELECT * FROM runs WHERE id = ?", parsed["run_id"])[0]
    assert (run["valid"], run["warning"], run["quarantined"], run["skipped"]) == (1, 6, 2, 1)


def test_unreadable_pages_never_reach_text(parsed, ws_root):
    row = q(ws_root, "SELECT text, structure FROM current_text WHERE locator = 'mixed.pdf'")[0]
    assert "ID-000123" not in row["text"]  # stamp on the rejected scan page
    assert "(cid:" not in row["text"]
    pages = [
        (b["page"], b["status"], b["start"] == b["end"])
        for b in json.loads(row["structure"])["blocks"]
    ]
    assert pages == [
        (1, "readable", False),
        (2, "unreadable", True),
        (3, "unreadable", True),
        (4, "readable", False),
        (5, "blank", True),
        (6, "unreadable", True),
    ]
    diag = q(ws_root, "SELECT diagnostics FROM outcome_issues WHERE locator = 'mixed.pdf'")[0]
    unreadable = next(d for d in json.loads(diag["diagnostics"]) if d["code"] == "unreadable_pages")
    assert unreadable["detail"]["pages"] == [2, 3, 6]


def test_fts_indexes_text_and_hits_map_to_blocks(parsed, ws_root):
    hits = q(
        ws_root,
        "SELECT ct.locator, ct.structure, ct.text FROM extracted_text_fts f"
        " JOIN current_text ct ON ct.extracted_text_id = f.rowid"
        " WHERE extracted_text_fts MATCH '\"merged across\"'",
    )
    assert [h["locator"] for h in hits] == ["report.docx"]
    at = hits[0]["text"].index("Merged across")
    block = next(
        b for b in json.loads(hits[0]["structure"])["blocks"] if b["start"] <= at < b["end"]
    )
    assert (block["kind"], block["heading_path"]) == ("table", ["Overview", "Scope"])
    stamp = q(
        ws_root, "SELECT rowid FROM extracted_text_fts WHERE extracted_text_fts MATCH '000123'"
    )
    assert stamp == []


# --- the definition of done -------------------------------------------------------


def test_rerun_with_no_changes_creates_no_rows(cli, parsed, ws_root):
    before = (count(ws_root, "extracted_text"), count(ws_root, "outcomes"))
    code, report = parse(cli, ws_root)
    assert (code, report["status"], report["unchanged"]) == (0, "noop", 10)
    assert (count(ws_root, "extracted_text"), count(ws_root, "outcomes")) == before


def test_second_parser_adds_rows_without_touching_the_first(cli, parsed, ws_root):
    first = q(ws_root, "SELECT id, text_hash FROM extracted_text ORDER BY id")
    code, report = parse(cli, ws_root, "--parser", "pdfium")
    assert code == 0
    assert (report["discovered"], report["warning"], report["quarantined"]) == (5, 3, 2)
    rows = q(ws_root, "SELECT id, text_hash, parser FROM extracted_text ORDER BY id")
    assert [{"id": r["id"], "text_hash": r["text_hash"]} for r in rows[: len(first)]] == first
    added = q(
        ws_root,
        "SELECT e.parser, i.locator FROM extracted_text e"
        " JOIN item_versions v ON v.id = e.item_version_id"
        " JOIN items i ON i.id = v.item_id WHERE e.parser = 'pdfium'"
        " ORDER BY i.locator",
    )
    assert [a["locator"] for a in added] == ["mixed.pdf", "native.pdf", "type3.pdf"]
    by_parser = q(
        ws_root,
        "SELECT parser, count(*) AS n FROM current_text"
        " WHERE locator = 'native.pdf' GROUP BY parser ORDER BY parser",
    )
    assert by_parser == [{"parser": "pdfium", "n": 1}, {"parser": "pdfplumber", "n": 1}]


def test_metadata_only_pdf_change_is_reported_as_metadata_only(cli, parsed, ws_root, corpus):
    set_pdf_title(corpus / "native.pdf", "A new title")
    (corpus / "table.csv").write_text("code;label\nZ9;changed\n", encoding="utf-8")
    code, ingest = cli.ingest(ws_root, corpus)
    assert (code, ingest["changed"]) == (0, 2)
    code, report = parse(cli, ws_root)
    assert (code, report["valid"] + report["warning"]) == (0, 2)

    versions = q(
        ws_root,
        "SELECT v.bytes_hash, e.text_hash, e.structure FROM item_versions v"
        " JOIN items i ON i.id = v.item_id"
        " JOIN extracted_text e ON e.item_version_id = v.id"
        " WHERE i.locator = 'native.pdf' ORDER BY v.id",
    )
    old, new = versions
    assert old["bytes_hash"] != new["bytes_hash"]
    assert old["text_hash"] == new["text_hash"]
    assert json.loads(new["structure"])["metadata"]["info"]["Title"] == "A new title"

    drift = q(
        ws_root,
        "SELECT locator, change, change_kind FROM drift WHERE run_id = ? ORDER BY locator",
        ingest["run_id"],
    )
    assert drift == [
        {"locator": "native.pdf", "change": "changed", "change_kind": "metadata_only"},
        {"locator": "table.csv", "change": "changed", "change_kind": "content"},
    ]


# --- incremental behaviour and fail-closed rules -----------------------------------


def test_limit_resumes_without_loss_or_double_count(cli, ws_root, corpus):
    cli.ingest(ws_root, corpus)
    code, first = parse(cli, ws_root, "--limit", "3")
    assert (code, first["remaining"]) == (0, 7)
    assert first["valid"] + first["warning"] + first["quarantined"] + first["skipped"] == 3
    code, second = parse(cli, ws_root)
    assert (second["unchanged"], second["remaining"]) == (3, 0)
    assert count(ws_root, "outcomes") == 10
    dupes = q(
        ws_root,
        "SELECT item_version_id FROM outcomes GROUP BY item_version_id, stage HAVING count(*) > 1",
    )
    assert dupes == []


def test_corrupt_file_is_not_retried(cli, parsed, ws_root):
    code, report = parse(cli, ws_root)
    assert report["status"] == "noop"
    broken = q(ws_root, "SELECT count(*) AS n FROM outcome_issues WHERE locator = 'broken.pdf'")
    assert broken == [{"n": 1}]


def test_all_quarantined_fails_the_run(cli, ws_root, tmp_path):
    src = tmp_path / "bad"
    src.mkdir()
    shutil.copy(CORPUS / "broken.pdf", src)
    shutil.copy(CORPUS / "scanned.pdf", src)
    cli.ingest(ws_root, src)
    code, report = parse(cli, ws_root)
    assert (code, report["status"], report["quarantined"]) == (1, "failed", 2)
    assert "AllQuarantined" in report["error"]


def test_empty_workspace_fails_closed_unless_permitted(cli, ws_root, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    cli.ingest(ws_root, empty, "--allow-empty")
    assert parse(cli, ws_root)[0] == 1
    code, report = parse(cli, ws_root, "--allow-empty")
    assert (code, report["status"]) == (0, "noop")


def test_missing_blob_fails_the_run(cli, ws_root, corpus):
    cli.ingest(ws_root, corpus)
    for blob in (ws_root / "blobs").rglob("*"):
        if blob.is_file():
            blob.parent.chmod(0o755)
            blob.unlink()
    code, report = parse(cli, ws_root)
    assert (code, report["status"]) == (1, "failed")
    assert "MissingBlob" in report["error"]


def test_parse_usage_errors(cli, ws_root, corpus):
    cli.ingest(ws_root, corpus)
    assert parse(cli, ws_root, "--parser", "nope")[0] == 2
    assert parse(cli, ws_root, "--source", str(corpus))[0] == 2
    assert parse(cli, ws_root, "--limit", "0")[0] == 2
    assert cli.ingest(ws_root, corpus, "--parser", "pdfium")[0] == 2
    assert count(ws_root, "outcomes") == 0


def test_current_text_marks_a_stale_fallback(parsed, ws_root):
    row = q(
        ws_root, "SELECT item_version_id, parser FROM current_text WHERE locator = 'native.pdf'"
    )[0]
    assert q(ws_root, "SELECT stale FROM current_text WHERE locator = 'native.pdf'") == [
        {"stale": 0}
    ]
    ws = open_workspace(ws_root)
    ws.conn.execute(  # a newer parser version ran and quarantined this item
        "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key, item_version_id,"
        " input_fp, stage_fp, params, outcome, reason_code, created_at)"
        " VALUES (1, ?, 'item', 'item:x', ?, 'b', 'newer', '{}', 'quarantined', 'parse_error', ?)",
        ("parse." + row["parser"], row["item_version_id"], utc_now()),
    )
    ws.close()
    assert q(ws_root, "SELECT stale FROM current_text WHERE locator = 'native.pdf'") == [
        {"stale": 1}
    ]


def test_outcome_ledger_refuses_fabricated_result_rows(parsed, ws_root):
    ws = open_workspace(ws_root)
    text_id = ws.conn.execute("SELECT id FROM extracted_text LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        ws.conn.execute(
            "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key, item_version_id,"
            " input_fp, stage_fp, params, outcome, reason_code, extracted_text_id, created_at)"
            " VALUES (1, 'parse.x', 'item', 'item:1', 1, 'b', 'y', '{}', 'quarantined', 'r', ?, ?)",
            (text_id, utc_now()),
        )
    ws.close()


# --- parsers on real content ------------------------------------------------------


def test_unmapped_glyphs_become_replacement_characters():
    for parse_fn in (parse_pdfplumber, parse_pdfium):
        doc = parse_fn(REAL / "cover_unmapped.pdf")
        cover = doc.text[doc.blocks[0].start : doc.blocks[0].end]
        assert "Department\ufffdof" in cover.replace(" \ufffd", "\ufffd"), parse_fn.__name__
        assert "(cid:" not in doc.text and "\x01" not in doc.text
        assert {i["code"] for i in doc.issues} >= {"unmapped_glyphs"}


def test_pdfium_hyphen_marker_becomes_a_hyphen():
    doc = parse_pdfium(REAL / "project_report.pdf")
    assert "minority-owned" in doc.text and "\ufffe" not in doc.text


def test_json_parser(tmp_path):
    array_doc = parse_json(REAL / "events.json")
    assert array_doc.metadata == {"top_level": "list"}
    assert len(json.loads(array_doc.text)) == 10
    path = tmp_path / "x.json"
    path.write_text('{"name": "Ren\\u00e9", "tags": ["a", "b"]}', encoding="utf-8")
    doc = parse_json(path)
    assert json.loads(doc.text) == {"name": "René", "tags": ["a", "b"]}
    assert doc.metadata == {"top_level": "dict"}
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        parse_json(path)


def test_outcomes_record_the_computed_parser_version(parsed, ws_root):
    rows = q(
        ws_root,
        "SELECT substr(o.stage, 7) AS tool, o.stage_fp AS tool_version, e.parser_version"
        " FROM outcomes o LEFT JOIN extracted_text e ON e.id = o.extracted_text_id"
        " WHERE o.stage LIKE 'parse.%'",
    )
    assert rows
    for r in rows:
        assert r["tool_version"] == PARSERS[r["tool"]].version
        assert r["parser_version"] in (None, r["tool_version"])


def test_blocks_tile_the_text_exactly(parsed, ws_root):
    from commissure.parse.model import SEPARATOR

    for row in q(ws_root, "SELECT locator, text, structure FROM current_text"):
        spans = [
            (b["start"], b["end"])
            for b in json.loads(row["structure"])["blocks"]
            if b["end"] > b["start"]
        ]
        assert spans[0][0] == 0 and spans[-1][1] == len(row["text"]), row["locator"]
        for (_, end), (start, _) in itertools.pairwise(spans):
            assert row["text"][end:start] == SEPARATOR, row["locator"]


def test_parser_version_tracks_libraries_and_source():
    pdf = PARSERS["pdfplumber"]
    assert pdf.version.startswith("pdfplumber=") and ";src=" in pdf.version
    wider = Parser(
        pdf.name, pdf.extensions, pdf.fn, (*pdf.modules, "commissure.runs"), pdf.libraries
    )
    assert wider.version != pdf.version
    assert "python=" in PARSERS["csv"].version


# --- schema migration -----------------------------------------------------------------


def test_migration_from_version_1_keeps_ledger_rows(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    conn = connect(root / DB_NAME)
    conn.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    now = utc_now()
    conn.execute(
        "INSERT INTO sources (kind, uri, role, created_at) VALUES ('folder','/x','corpus',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO runs (step, trigger, status, started_at, config)"
        " VALUES ('ingest','manual','succeeded',?,'{}')",
        (now,),
    )
    conn.execute("INSERT INTO items (source_id, locator) VALUES (1, 'a.pdf')")
    conn.execute(
        "INSERT INTO item_versions (item_id, run_id, bytes_hash, text_hash, size_bytes,"
        " role, is_current, created_at) VALUES (1, 1, 'h', 't', 1, 'corpus', 1, ?)",
        (now,),
    )
    conn.close()

    ws = open_workspace(root)
    assert ws.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    columns = {r["name"] for r in ws.conn.execute("PRAGMA table_info(item_versions)")}
    assert "text_hash" not in columns and "bytes_hash" in columns
    assert [dict(r) for r in ws.conn.execute("SELECT locator, bytes_hash FROM current_items")] == [
        {"locator": "a.pdf", "bytes_hash": "h"}
    ]
    ws.close()


def test_new_parser_version_reextracts_and_becomes_current(cli, parsed, ws_root):
    ws = open_workspace(ws_root)  # pretend every outcome came from an older parser build
    ws.conn.execute("UPDATE outcomes SET stage_fp = 'old' WHERE stage LIKE 'parse.%'")
    ws.conn.execute("UPDATE extracted_text SET parser_version = 'old'")
    ws.close()
    texts_before = count(ws_root, "extracted_text")
    code, report = parse(cli, ws_root)
    assert (code, report["unchanged"]) == (0, 1)  # only notes.txt, which has no parser
    assert count(ws_root, "extracted_text") == texts_before * 2
    current = q(ws_root, "SELECT parser_version, stale FROM current_text")
    assert current and all(r["parser_version"] != "old" and r["stale"] == 0 for r in current)


def test_parse_reads_the_ingested_bytes_not_the_source(cli, ws_root, corpus):
    original = (corpus / "table.csv").read_text(encoding="utf-8-sig")
    cli.ingest(ws_root, corpus)
    (corpus / "table.csv").write_text("edited;after;ingest\n", encoding="utf-8")
    parse(cli, ws_root)
    text = q(ws_root, "SELECT text FROM current_text WHERE locator = 'table.csv'")[0]["text"]
    assert text.startswith("code\tlabel\tqty") and "edited" not in text
    assert original.startswith("code;label;qty")


def test_docx_merged_cells_appear_once(tmp_path):
    from docx import Document

    from commissure.parse.office import parse_docx

    doc = Document()
    table = doc.add_table(rows=3, cols=3)
    for r in range(3):
        for c in range(3):
            table.cell(r, c).text = f"{r}{c}"
    table.cell(0, 0).merge(table.cell(0, 1)).text = "wide"
    table.cell(1, 2).merge(table.cell(2, 2)).text = "tall"
    path = tmp_path / "m.docx"
    doc.save(path)
    assert parse_docx(path).tables[0]["rows"] == [
        ["wide", None, "02"],
        ["10", "11", "tall"],
        ["20", "21", None],
    ]


def test_re_added_identical_file_gets_text_for_its_new_version(cli, parsed, ws_root, corpus):
    content = (corpus / "table.csv").read_bytes()
    (corpus / "table.csv").unlink()
    cli.ingest(ws_root, corpus)
    (corpus / "table.csv").write_bytes(content)
    cli.ingest(ws_root, corpus)
    parse(cli, ws_root)
    rows = q(ws_root, "SELECT parser FROM current_text WHERE locator = 'table.csv'")
    assert rows == [{"parser": "csv"}]


def test_every_pdfium_call_holds_the_lock(monkeypatch):
    """PDFium is not thread-safe and parse runs on a thread pool (it segfaulted before
    the lock): every call into pypdfium2 must happen while parse/pdf.py holds it."""
    import commissure.parse.pdf as pdf_module

    calls = []

    def guard(obj):
        return Guard(obj) if type(obj).__module__.startswith("pypdfium2") else obj

    class Guard:
        def __init__(self, obj):
            self._obj = obj

        def __getattr__(self, name):
            attr = getattr(self._obj, name)
            if not callable(attr):
                return attr

            def call(*args, **kwargs):
                calls.append((name, pdf_module._PDFIUM.locked()))
                return guard(attr(*args, **kwargs))

            return call

        def __getitem__(self, index):
            calls.append(("__getitem__", pdf_module._PDFIUM.locked()))
            return guard(self._obj[index])

    real = pdf_module.pypdfium2.PdfDocument

    def document(*args, **kwargs):
        calls.append(("PdfDocument", pdf_module._PDFIUM.locked()))
        return Guard(real(*args, **kwargs))

    monkeypatch.setattr(pdf_module.pypdfium2, "PdfDocument", document)
    parse_pdfium(REAL / "project_report.pdf")
    names = {name for name, _ in calls}
    assert {"PdfDocument", "__getitem__", "get_textpage", "get_text_range", "close"} <= names
    assert [c for c in calls if not c[1]] == []


def test_word_gaps_are_font_relative():
    """A gap of 0.22 em separates words. With pdfplumber's absolute 3-point default the
    letter-spaced title of this real sample came out as 'PRO JECT RE PO RT', and PDFs that
    draw no space glyphs (a quarter-em gap instead) lost every inter-word space."""
    doc = parse_pdfplumber(REAL / "project_report.pdf")
    first_page = doc.text[doc.blocks[0].start : doc.blocks[0].end]
    assert "PROJECT REPORT" in first_page
    assert "PRO JECT" not in first_page


def test_xlsx_timedelta_and_non_json_cells_parse_and_store(cli, ws_root, tmp_path):
    """Excel duration formats (`[hh]:mm:ss`) yield `datetime.timedelta`, and raw numeric
    overflow (`1e500`) yields `inf`. Every cell in `structure.tables` must normalize to
    an RFC 8259 JSON primitive matching `doc.text` and satisfy `json_valid(structure)`."""
    import datetime as dt
    import zipfile
    from decimal import Decimal

    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    from commissure.parse.model import rows_to_text
    from commissure.parse.tabular import _value

    src = tmp_path / "sheets"
    src.mkdir()
    raw_xlsx = tmp_path / "raw.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Durations"
    ws.append(["label", "elapsed", "active", "count", "reading"])
    ws.append(["multi_day", dt.timedelta(days=1, seconds=58246), True, 7, 999.25])
    ws.append(["sub_day", dt.timedelta(hours=2, minutes=30), False, 0, "   "])
    wb.save(raw_xlsx)

    # Patch 999.25 -> 1e500 in worksheet XML so openpyxl `_cast_number` returns `inf`.
    xlsx_path = src / "durations.xlsx"
    with zipfile.ZipFile(raw_xlsx, "r") as zin, zipfile.ZipFile(xlsx_path, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b"<v>999.25</v>", b"<v>1e500</v>")
            zout.writestr(item, data)

    assert cli.ingest(ws_root, src)[0] == 0
    code, report = parse(cli, ws_root)
    assert (code, report["status"], report["valid"], report["quarantined"]) == (
        0,
        "succeeded",
        1,
        0,
    )

    stored = q(ws_root, "SELECT text, structure FROM current_text WHERE locator = 'durations.xlsx'")
    assert len(stored) == 1
    structure = json.loads(stored[0]["structure"])
    rows = structure["tables"][0]["rows"]
    assert rows == [
        ["label", "elapsed", "active", "count", "reading"],
        ["multi_day", "1 day, 16:10:46", True, 7, "inf"],
        ["sub_day", "2:30:00", False, 0, None],
    ]
    assert type(rows[1][2]) is bool and type(rows[1][3]) is int
    assert stored[0]["text"] == rows_to_text(rows)

    assert _value(float("nan")) == "nan"
    assert _value(float("-inf")) == "-inf"
    assert _value(Decimal("12.34")) == "12.34"
    assert _value(b"hello") == "hello"
    assert _value(b"   ") is None
    assert _value(CellRichText([TextBlock(InlineFont(), "bold"), " text"])) == "bold text"
    assert _value(ArrayFormula(ref="A1:A2", text="=SUM(B1:B2)")) is None
    assert _value(DataTableFormula(ref="A1:B2")) is None


def test_non_serializable_or_nan_structure_quarantines_item(cli, ws_root, tmp_path, monkeypatch):
    """Item-level structure serialization errors (`TypeError`, `NaN`/`Infinity`, or lone
    UTF-16 surrogates rejected by RFC 8259 / SQLite `json_valid`) must quarantine that
    item under `parse_error` rather than crashing the parse run."""
    from commissure.parse.model import Block, ParsedDoc

    src = tmp_path / "items"
    src.mkdir()
    (src / "good.json").write_text('{"kind": "good"}', encoding="utf-8")
    (src / "bad_obj.json").write_text('{"kind": "obj"}', encoding="utf-8")
    (src / "bad_nan.json").write_text('{"kind": "nan"}', encoding="utf-8")
    (src / "bad_surrogate.json").write_text('{"kind": "surrogate"}', encoding="utf-8")

    def faulty_json(path: Path) -> ParsedDoc:
        kind = json.loads(path.read_text(encoding="utf-8"))["kind"]
        if kind == "good":
            return parse_json(path)
        meta = {
            "obj": {"leak": object()},
            "nan": {"score": float("nan")},
            "surrogate": {"bad": "\ud800"},
        }[kind]
        return ParsedDoc(
            text="readable body",
            blocks=[Block(kind="document", start=0, end=13)],
            tables=[],
            metadata=meta,
            issues=[],
        )

    orig = PARSERS["json"]
    monkeypatch.setitem(
        PARSERS,
        "json",
        Parser(orig.name, orig.extensions, faulty_json, orig.modules, orig.libraries, orig.stdlib),
    )

    assert cli.ingest(ws_root, src)[0] == 0
    code, report = parse(cli, ws_root)
    assert (code, report["status"], report["valid"], report["quarantined"]) == (
        0,
        "succeeded",
        1,
        3,
    )
    assert [r["locator"] for r in q(ws_root, "SELECT locator FROM current_text")] == ["good.json"]
    issues = {
        r["locator"]: (r["outcome"], r["reason_code"], json.loads(r["diagnostics"])[0]["message"])
        for r in q(ws_root, "SELECT locator, outcome, reason_code, diagnostics FROM outcome_issues")
    }
    assert set(issues) == {"bad_obj.json", "bad_nan.json", "bad_surrogate.json"}
    assert issues["bad_obj.json"][:2] == ("quarantined", "parse_error")
    assert "TypeError" in issues["bad_obj.json"][2]
    assert issues["bad_nan.json"][:2] == ("quarantined", "parse_error")
    assert "ValueError" in issues["bad_nan.json"][2]
    assert issues["bad_surrogate.json"][:2] == ("quarantined", "parse_error")
    assert "UnicodeEncodeError" in issues["bad_surrogate.json"][2]
