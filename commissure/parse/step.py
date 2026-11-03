"""`run --step parse`: extract text and structure from current items, on the runner.

Each parser is its own stage (`parse.pdfplumber`, `parse.docx`, ...); items with no
parser for their type are skipped under stage `parse`. Parse runs in cache mode: an
item version is done once it has an outcome under the parser's computed version, so
reruns are incremental and a new parser version re-extracts.

Scope is every current item, or, with an explicit parser, the current items that
parser handles. Bytes come from the blob store, never from the source folder.

Outcomes:
    valid        text extracted, no issues
    warning      text extracted, with issues (unreadable pages, uncached formulas, ...)
    quarantined  no usable text, an item resource limit was exceeded, or the parser raised
                 (a bad file is an expected data condition here, not a fault); no
                 extracted_text row
    skipped      no parser for the item's type

Because an outcome means "done under this key", a run that fails because every item was
quarantined is followed by a clean `noop`: those items are not retried until the parser
changes (or a limit that quarantined an item is raised). The failure stays visible in
the ledger and in `outcome_issues`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from commissure.limits import DEFAULT_LIMITS, LimitExceeded, Limits, check_file_bytes
from commissure.parse import (
    BUILTIN_PARSERS,
    DEFAULT_FOR,
    DEFAULT_PARSERS,
    PARSERS,
    Parser,
    extension,
)
from commissure.parse.model import ParsedDoc
from commissure.parse.pool import ParseProcessPool
from commissure.records import Diagnostic, RecordEnvelope
from commissure.runner import Plan, RunContext, Subject, canonical
from commissure.workspace import Workspace, utc_now

STEP = "parse"


class MissingBlob(Exception):
    """An item's bytes are not in the blob store: the workspace is damaged."""


def judge_doc(doc: ParsedDoc) -> RecordEnvelope[ParsedDoc]:
    diagnostics = [
        Diagnostic(code=i["code"], detail={k: v for k, v in i.items() if k != "code"})
        for i in doc.issues
    ]
    codes = sorted({i["code"] for i in doc.issues})
    if not doc.text.strip():
        return RecordEnvelope.quarantined(",".join(["no_readable_text", *codes]), *diagnostics)
    if codes:
        return RecordEnvelope.warning(doc, ",".join(codes), *diagnostics)
    return RecordEnvelope.valid(doc)


def _parser_for(locator: str, parser_name: str | None) -> Parser | None:
    if parser_name:
        return PARSERS[parser_name]
    name = DEFAULT_FOR.get(extension(locator))
    return PARSERS[name] if name else None


def _serialize_json(value: object) -> str:
    # SQLite enforces CHECK (json_valid(...)), which follows RFC 8259 and rejects
    # Python's default NaN/Infinity literals and unpaired UTF-16 surrogates.
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    raw.encode("utf-8")
    return raw


def _is_monkeypatched_for_test(parser_name: str) -> bool:
    """True when an in-process test monkeypatched `PARSERS` or `pytesseract` with a local fn."""
    if PARSERS.get(parser_name) is not BUILTIN_PARSERS.get(parser_name):
        return True
    if parser_name == "ocr":
        pytess = sys.modules.get("pytesseract")
        fn = getattr(pytess, "image_to_data", None) if pytess is not None else None
        if "<locals>" in getattr(fn, "__qualname__", ""):
            return True
    return False


def execute_parse_local(
    parser_name: str,
    blob: Path,
    limits: Limits = DEFAULT_LIMITS,
    *,
    _test_sleep_s: float = 0.0,
) -> RecordEnvelope:
    """Run one parser against `blob` and return a serializable `RecordEnvelope`."""
    try:
        if _test_sleep_s > 0:
            time.sleep(_test_sleep_s)
        check_file_bytes(blob, limits)
        parser = PARSERS[parser_name]
        doc = parser.parse(blob, limits)
        if parser.name == "ocr" and doc.text.strip():
            codes = {i["code"] for i in doc.issues}
            if "ocr_pages" not in codes and "unreadable_pages" not in codes:
                return RecordEnvelope.skipped(
                    "no_unreadable_pages", Diagnostic(code="no_unreadable_pages")
                )
        env = judge_doc(doc)
        _serialize_json([d.model_dump(exclude_defaults=True) for d in env.diagnostics])
        if env.payload is None:
            return env
        structure_json = _serialize_json(doc.structure())
        extracted_bytes = len(doc.text.encode("utf-8")) + len(structure_json.encode("utf-8"))
        if extracted_bytes > limits.max_extracted_bytes:
            raise LimitExceeded(
                "extracted_output_too_large",
                f"extracted text + structure ({extracted_bytes} bytes) exceeds"
                f" max_extracted_bytes ({limits.max_extracted_bytes})",
                actual=extracted_bytes,
                limit=limits.max_extracted_bytes,
            )
        payload = (doc.text, doc.text_hash, structure_json)
    except LimitExceeded as exc:
        return RecordEnvelope.quarantined(
            exc.code,
            Diagnostic(code=exc.code, message=str(exc), detail=exc.detail),
        )
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}".encode("utf-8", errors="backslashreplace").decode()
        return RecordEnvelope.quarantined(
            "parse_error", Diagnostic(code="parse_error", message=msg)
        )
    return RecordEnvelope(
        outcome=env.outcome,
        payload=payload,
        reason_code=env.reason_code,
        diagnostics=env.diagnostics,
    )


def _store(conn: sqlite3.Connection, run_id: int, s: Subject, env: RecordEnvelope) -> dict:
    parser, _ = s.input
    text, text_hash, structure = env.payload
    text_id = conn.execute(
        "INSERT INTO extracted_text (item_version_id, parser, parser_version, text, text_hash,"
        " structure, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (s.item_version_id, parser.name, s.stage_fp, text, text_hash,
         structure, run_id, utc_now()),
    ).lastrowid  # fmt: skip
    return {"extracted_text_id": text_id}


def parse_plan(
    ws: Workspace,
    parser_name: str | None,
    *,
    allow_empty: bool = False,
    limits: Limits = DEFAULT_LIMITS,
    workers: int = 1,
) -> Plan:
    """`parser_name` must already be validated against PARSERS."""
    scope = [PARSERS[parser_name]] if parser_name else [PARSERS[n] for n in DEFAULT_PARSERS]
    proc_pool = ParseProcessPool(max_workers=max(1, workers), limits=limits)

    def _execute(ctx: RunContext, s: Subject) -> RecordEnvelope:
        parser, blob = s.input
        try:
            check_file_bytes(blob, limits)
        except LimitExceeded as exc:
            return RecordEnvelope.quarantined(
                exc.code,
                Diagnostic(code=exc.code, message=str(exc), detail=exc.detail),
            )
        if _is_monkeypatched_for_test(parser.name):
            return execute_parse_local(parser.name, blob, limits)
        return proc_pool.parse_item(parser.name, blob, cancel_event=ctx.cancel_event)

    def subjects() -> list[Subject]:
        rows = ws.conn.execute(
            "SELECT item_id, item_version_id, locator, bytes_hash FROM current_items"
            " ORDER BY item_version_id"
        ).fetchall()
        if parser_name:
            exts = PARSERS[parser_name].extensions
            rows = [r for r in rows if extension(r["locator"]) in exts]
        out = []
        for r in rows:
            parser = _parser_for(r["locator"], parser_name)
            blob: Path = ws.blobs.path_for(r["bytes_hash"])
            skip = None
            if parser is None:
                skip = RecordEnvelope.skipped(
                    "no_parser",
                    Diagnostic(code="no_parser", detail={"extension": extension(r["locator"])}),
                )
            elif not blob.is_file():
                raise MissingBlob(f"no blob {r['bytes_hash']} for {r['locator']}")
            out.append(Subject(
                stage=f"{STEP}.{parser.name}" if parser else STEP,
                stage_fp=parser.version if parser else "",
                kind="item", key=f"item:{r['item_id']}", item_version_id=r["item_version_id"],
                input=(parser, blob), input_fp=r["bytes_hash"], skip=skip,
            ))  # fmt: skip
        return out

    return Plan(
        step=STEP,
        stage_fp=canonical({p.name: p.version for p in scope}),
        params={},
        subjects=subjects,
        execute=_execute,
        store=_store,
        cache=True,
        allow_empty=allow_empty,
        allow_nothing_attempted=(parser_name == "ocr"),
        limits=limits,
        close=proc_pool.close,
    )
