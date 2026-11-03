"""Evidence: core checks and resolves the pointers recipe payloads carry.

Driven through the CLI with the demo recipe's `cite` (points at a needle in an item's
text, or breaks the pointer on purpose) and `carry` (copies upstream evidence into a
group record). Workspaces come from `open_workspace`; the OCR text row in
`ocr_item` is inserted into one so page and confidence resolution runs without
Tesseract.
"""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from commissure.workspace import open_workspace, utc_now

DEMO = Path(__file__).parent / "recipes" / "demo"
PROBE = Path(__file__).parent / "fixtures" / "probe_corpus"
OCR_CORPUS = Path(__file__).parent / "fixtures" / "ocr_corpus"
HAVE_TESSERACT = shutil.which("tesseract") is not None
needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract binary not installed")

PAGES = (
    "Page one is native text with no recognition at all.",
    "Page two was recognised by OCR from a clean scan.",
    "Page three is a faint scan recognised by OCR too.",
)
OCR_PARSER_VERSION = "test-ocr-1"


def q(ws_root, sql, *params):
    ws = open_workspace(ws_root)
    try:
        return [dict(r) for r in ws.conn.execute(sql, params)]
    finally:
        ws.close()


def run(cli, ws_root, step, *params):
    args = ["run", "--step", step, "--workspace", str(ws_root), "--recipe", str(DEMO)]
    for p in params:
        args += ["--param", p]
    return cli(*args, "--fresh", "--json")


def cited(cli, ws_root, *params, step="cite"):
    """Run `step`; return its one record's stored payload and its evidence rows."""
    res = run(cli, ws_root, step, *params)
    assert res.exit_code == 0, res.output
    run_id = json.loads(res.stdout)["run_id"]
    [record] = q(
        ws_root,
        "SELECT r.id, r.payload, r.payload_hash FROM records r"
        " JOIN outcomes o ON o.record_id = r.id WHERE o.run_id = ?",
        run_id,
    )
    rows = q(ws_root, "SELECT * FROM evidence WHERE record_id = ? ORDER BY id", record["id"])
    return record, json.loads(record["payload"]), rows


@pytest.fixture
def json_item(cli, ws_root, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "good.json").write_text('{"name": "good", "amount": 10}', encoding="utf-8")
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    return src


@pytest.fixture
def ocr_item(cli, ws_root, tmp_path):
    """A .pdf item whose current text is a three-page OCR row: page 1 native, page 2
    recognised at 87.5, page 3 at 60.0, with the matching `ocr_pages` diagnostic."""
    src = tmp_path / "scans"
    src.mkdir()
    (src / "letter.pdf").write_bytes(b"%PDF-1.4 stand-in bytes; the text row below is the test")
    assert cli.ingest(ws_root, src)[0] == 0
    text, blocks, at = "", [], 0
    for page, (body, flags, confidence) in enumerate(
        zip(PAGES, ([], ["ocr"], ["ocr"]), (None, 87.5, 60.0), strict=True), start=1
    ):
        if text:
            text += "\n\n"
            at += 2
        block = {"kind": "page", "start": at, "end": at + len(body), "page": page}
        if flags:
            block |= {"flags": flags, "confidence": confidence}
        blocks.append(block)
        text += body
        at += len(body)
    ws = open_workspace(ws_root)
    try:
        conn = ws.conn
        item_id, version_id = conn.execute(
            "SELECT item_id, item_version_id FROM current_items WHERE locator = 'letter.pdf'"
        ).fetchone()
        run_id = conn.execute(
            "INSERT INTO runs (step, trigger, status, started_at, finished_at, config, remaining)"
            " VALUES ('parse', 'manual', 'succeeded', ?, ?, '{}', 0)",
            (utc_now(), utc_now()),
        ).lastrowid
        text_id = conn.execute(
            "INSERT INTO extracted_text (item_version_id, parser, parser_version, text,"
            " text_hash, run_id, created_at, structure) VALUES (?, 'ocr', ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                OCR_PARSER_VERSION,
                text,
                hashlib.sha256(text.encode()).hexdigest(),
                run_id,
                utc_now(),
                json.dumps({"blocks": blocks}),
            ),
        ).lastrowid
        diagnostics = [
            {"code": "ocr_pages", "detail": {"pages": [2, 3], "confidence": {"2": 87.5, "3": 60.0}}}
        ]
        conn.execute(
            "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key, item_version_id,"
            " input_fp, stage_fp, params, outcome, reason_code, diagnostics, extracted_text_id,"
            " created_at) VALUES (?, 'parse.ocr', 'item', ?, ?, 'x', ?, '{}', 'warning',"
            " 'ocr_pages', ?, ?, ?)",
            (
                run_id,
                f"item:{item_id}",
                version_id,
                OCR_PARSER_VERSION,
                json.dumps(diagnostics),
                text_id,
                utc_now(),
            ),
        )
    finally:
        ws.close()
    return src


def test_a_span_is_resolved_into_the_stored_payload_and_indexed(cli, ws_root, json_item):
    record, payload, [row] = cited(cli, ws_root, "needle=good")
    [ev] = payload["evidence"]
    text = q(
        ws_root,
        "SELECT extracted_text_id AS id, text FROM current_text WHERE locator = 'good.json'",
    )[0]
    assert text["text"][ev["start"] : ev["end"]] == ev["text"] == "good"
    assert ev["extracted_text_id"] == text["id"]
    # Core filled these in; the JSON document has no pages and no OCR.
    assert (ev["locator"], ev["page"], ev["via_ocr"], ev["confidence"]) == (
        "good.json",
        None,
        False,
        None,
    )
    # The hash covers the resolved payload, so downstream sees what was stored.
    assert record["payload_hash"] == hashlib.sha256(record["payload"].encode()).hexdigest()
    assert (row["pointer"], row["kind"], row["copied"]) == ("/evidence/0", "quote", 0)
    assert (row["span_start"], row["span_end"]) == (ev["start"], ev["end"])
    [review] = q(ws_root, "SELECT * FROM evidence_review WHERE origin = 'record'")
    assert (review["span_text"], review["locator"], review["stage"]) == (
        "good",
        "good.json",
        "demo.cite",
    )


def test_page_ocr_and_confidence_come_from_the_text_structure(cli, ws_root, ocr_item):
    def resolved(*params):
        return cited(cli, ws_root, *params)[1]["evidence"][0]

    native = resolved(f"needle={json.dumps(PAGES[0][:20])}")
    assert (native["page"], native["via_ocr"], native["confidence"]) == (1, False, None)
    scanned = resolved(f"needle={json.dumps(PAGES[1][10:40])}")
    assert (scanned["page"], scanned["via_ocr"], scanned["confidence"]) == (2, True, 87.5)
    # A span across pages 2 and 3 starts on page 2 and takes the lower confidence.
    across = resolved(f"needle={json.dumps(PAGES[1][-12:] + chr(10) * 2 + PAGES[2][:10])}")
    assert (across["page"], across["via_ocr"], across["confidence"]) == (2, True, 60.0)
    page3 = resolved('needle="Page"', "mode=page", "page=3")
    assert (page3["page"], page3["via_ocr"], page3["confidence"]) == (3, True, 60.0)
    assert page3["start"] is None and page3["text"] is None
    whole = resolved('needle="Page"', "mode=whole")
    assert (whole["page"], whole["via_ocr"], whole["confidence"]) == (None, True, None)

    # The parse finding is page evidence too, read from its recorded diagnostic.
    parse_rows = q(
        ws_root,
        "SELECT page, kind, via_ocr, confidence FROM evidence_review"
        " WHERE origin = 'parse' ORDER BY page",
    )
    assert parse_rows == [
        {"page": 2, "kind": "ocr_pages", "via_ocr": 1, "confidence": 87.5},
        {"page": 3, "kind": "ocr_pages", "via_ocr": 1, "confidence": 60.0},
    ]


def test_a_page_the_text_does_not_have_fails_the_run(cli, ws_root, ocr_item):
    res = run(cli, ws_root, "cite", 'needle="Page"', "mode=page", "page=9")
    assert res.exit_code == 1
    assert "has no page 9" in res.output


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("bad_slice", "claims 'not good' but good.json"),
        ("foreign", "not part of this subject"),
        ("other_text", "the stage was given"),
        ("out_of_range", "is outside good.json"),
        ("recipe_page", "sets a page on a span"),
        ("recipe_confidence", "sets locator or confidence"),
        ("forged", "matches no evidence in a consumed upstream record"),
    ],
)
def test_evidence_that_does_not_check_out_fails_the_run_and_stores_nothing(
    cli, ws_root, json_item, mode, message
):
    res = run(cli, ws_root, "cite", "needle=good", f"mode={mode}")
    assert res.exit_code == 1, res.output
    assert message in res.output
    assert q(ws_root, "SELECT count(*) AS n FROM evidence")[0]["n"] == 0
    assert q(ws_root, "SELECT count(*) AS n FROM records WHERE stage = 'demo.cite'")[0]["n"] == 0


def test_copied_upstream_evidence_is_accepted_and_altered_copies_are_not(cli, ws_root, json_item):
    _, cite_payload, _ = cited(cli, ws_root, "needle=good")
    _, carry_payload, [row] = cited(cli, ws_root, step="carry")
    assert carry_payload["evidence"] == cite_payload["evidence"]
    assert (row["copied"], row["pointer"]) == (1, "/evidence/0")

    res = run(cli, ws_root, "carry", "mode=alter")
    assert res.exit_code == 1
    assert "matches no evidence in a consumed upstream record" in res.output


def test_evidence_rows_are_never_modified_and_a_rerun_adds_new_ones(cli, ws_root, json_item):
    _, _, [first] = cited(cli, ws_root, "needle=good")
    _, _, [second] = cited(cli, ws_root, "needle=good")
    assert second["id"] != first["id"]
    assert q(ws_root, "SELECT * FROM evidence WHERE id = ?", first["id"]) == [first]
    ws = open_workspace(ws_root)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="never modified"):
            ws.conn.execute("UPDATE evidence SET page = 7 WHERE id = ?", (first["id"],))
    finally:
        ws.close()


def test_real_pdf_pages_resolve_and_parse_findings_are_page_evidence(cli, ws_root, tmp_path):
    """Generated probe PDFs, parsed for real: mixed.pdf page 4 has a readable text layer,
    scanned.pdf is quarantined with every page image-only (no text row)."""
    src = tmp_path / "src"
    src.mkdir()
    for name in ("mixed.pdf", "scanned.pdf"):
        shutil.copy(PROBE / name, src / name)
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    [mixed] = q(ws_root, "SELECT text, structure FROM current_text WHERE locator = 'mixed.pdf'")
    page4 = next(b for b in json.loads(mixed["structure"])["blocks"] if b.get("page") == 4)
    # The fixture's pages cycle the same words, so search from where page 4 starts.
    needle = mixed["text"][page4["start"] : page4["start"] + 40]

    found = cited(cli, ws_root, f"needle={json.dumps(needle)}", f"from={page4['start']}")
    ev = found[1]["evidence"][0]
    assert ev["start"] == page4["start"]
    assert (ev["locator"], ev["page"], ev["via_ocr"]) == ("mixed.pdf", 4, False)

    scanned = q(
        ws_root,
        "SELECT page, kind, outcome FROM evidence_review"
        " WHERE origin = 'parse' AND locator = 'scanned.pdf' ORDER BY page",
    )
    assert scanned == [
        {"page": 1, "kind": "unreadable_pages", "outcome": "quarantined"},
        {"page": 2, "kind": "unreadable_pages", "outcome": "quarantined"},
    ]


@needs_tesseract
def test_recognised_text_carries_ocr_and_confidence_end_to_end(cli, ws_root, tmp_path):
    src = tmp_path / "src"
    shutil.copytree(OCR_CORPUS, src)
    assert cli.ingest(ws_root, src)[0] == 0
    assert (
        cli("run", "--step", "parse", "--workspace", str(ws_root), "--parser", "ocr").exit_code == 0
    )
    ev = cited(cli, ws_root, 'needle="Budget"')[1]["evidence"][0]
    assert (ev["locator"], ev["page"], ev["via_ocr"]) == ("typed_letter.pdf", 1, True)
    assert isinstance(ev["confidence"], float) and 0 < ev["confidence"] <= 100


def test_inspect_subject_lists_evidence_with_context_links_and_what_it_supports(
    cli, ws_root, ocr_item
):
    """`inspect --subject` shows each pointer against the original: span in context, the
    payload object it supports, a page link, and parse page evidence from diagnostics."""
    needle = PAGES[1][10:40]

    def evidence(*extra):
        res = cli("inspect", "--workspace", str(ws_root), "--subject", "letter.pdf", *extra)
        assert res.exit_code == 0, res.output
        return res

    cited(cli, ws_root, f"needle={json.dumps(needle)}")
    detail = json.loads(evidence("--json").stdout)["workspaces"][0]["subject_detail"]
    [record] = [e for e in detail["evidence"] if e["origin"] == "record"]
    assert (record["pointer"], record["kind"], record["page"]) == ("/evidence/0", "quote", 2)
    assert (record["via_ocr"], record["confidence"]) == (True, 87.5)
    assert record["supports"] == {"quote": needle}
    text = PAGES[0] + "\n\n" + PAGES[1]
    at = text.index(needle)
    assert record["span_text"] == needle
    assert record["context_before"] == text[max(0, at - 80) : at]
    assert (
        record["context_after"] == PAGES[1][40:] + "\n\n" + PAGES[2][: 80 - len(PAGES[1][40:]) - 2]
    )
    assert record["url"] == f"{(ocr_item / 'letter.pdf').as_uri()}#page=2"
    parse = [(e["page"], e["kind"]) for e in detail["evidence"] if e["origin"] == "parse"]
    assert parse == [(2, "ocr_pages"), (3, "ocr_pages")]

    human = evidence().stdout
    assert "evidence demo.cite /evidence/0  quote  letter.pdf p. 2  [ocr, confidence 87.5]" in human
    assert f"  {record['url']}" in human.splitlines()

    # Item-level evidence says it has no exact span; a changed original loses its link.
    cited(cli, ws_root, 'needle="Page"', "mode=whole")
    (ocr_item / "letter.pdf").write_bytes(b"replaced")
    detail = json.loads(evidence("--json").stdout)["workspaces"][0]["subject_detail"]
    [whole] = [e for e in detail["evidence"] if e["origin"] == "record"]
    assert (whole["span_text"], whole["context_before"], whole["url"]) == (None, None, None)
    assert whole["original_status"] == "changed"
    human = evidence().stdout
    assert "(the item as a whole: no exact span; search the original for it)" in human
    assert f"original {ocr_item / 'letter.pdf'} is changed" in human
