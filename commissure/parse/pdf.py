"""PDF parsers. Both run the probe's readability gate on every page, and text from a
page the gate rejects never enters `text`: the page is listed as a block with its
status and reasons instead.

- `pdfplumber`: layout-ordered text and ruled-table detection (pdfminer underneath).
- `pdfium`: PDFium's text in content-stream order; much faster text, no tables. The
  gate still runs through pdfplumber, so the speed gain is in extraction only. PDFium
  is not thread-safe, so its calls are serialized across the runner's workers.

Undecodable glyphs become U+FFFD (unknown character), never a guessed character.

`_parse` also carries the hook the `ocr` parser (`commissure/parse/ocr.py`) uses to
recognise the pages the gate rejects; without the hook those pages stay empty blocks.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import pypdfium2

from commissure.limits import DEFAULT_LIMITS, LimitExceeded, Limits, check_file_bytes
from commissure.parse.model import DocBuilder, ParsedDoc
from commissure.probe.pdf import (
    PDFIUM_LOCK,
    assess_page,
    check_pdf_page_count,
    iter_pdf_pages,
    open_pdf,
)

CID = re.compile(r"\(cid:\d+\)")


def _metadata(info: dict[str, Any], pages: int) -> dict[str, Any]:
    clean = {}
    for k, v in sorted(info.items()):
        clean[k] = v if isinstance(v, str | int | float | bool) else str(v)
    return {"pages": pages, "info": clean}


def _issues(
    b: DocBuilder, pages: dict[int, tuple[str, list[str], list[str], float | None]]
) -> None:
    """Issues from the final per-page status (after any OCR), in a fixed order."""
    unreadable = {n: r for n, (status, r, _, _) in pages.items() if status == "unreadable"}
    if unreadable:
        b.issue(
            "unreadable_pages",
            pages=sorted(unreadable),
            reasons={str(n): r for n, r in sorted(unreadable.items())},
        )
    for flag in ("unmapped_glyphs", "full_page_image_under_text"):
        flagged = sorted(n for n, (_, _, flags, _) in pages.items() if flag in flags)
        if flagged:
            b.issue(flag, pages=flagged)
    recognised = {n: c for n, (_, _, flags, c) in pages.items() if "ocr" in flags}
    if recognised:
        b.issue(
            "ocr_pages",
            pages=sorted(recognised),
            confidence={str(n): c for n, c in sorted(recognised.items())},
        )
    b.metadata["blank_pages"] = sorted(n for n, (s, _, _, _) in pages.items() if s == "blank")


def _parse(
    path: Path,
    page_text,
    ocr_page=None,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> ParsedDoc:
    """Walk the pages through the gate. `page_text(page, number)` extracts a readable page;
    `ocr_page(number)`, when given, returns (text, confidence, reason, flags) for an
    unreadable one, where a reason means the page stays unreadable."""
    check_file_bytes(path, limits)
    check_pdf_page_count(path, limits.max_pdf_pages)
    b = DocBuilder()
    pages: dict[int, tuple[str, list[str], list[str], float | None]] = {}
    assessed: list[tuple[int, str, list[str], list[str], str, list[Any]]] = []
    with open_pdf(path) as pdf:
        for number, page in iter_pdf_pages(pdf, max_pages=limits.max_pdf_pages):
            _, verdict, _ = assess_page(page)
            status, reasons, flags = verdict.verdict, list(verdict.reasons), list(verdict.flags)
            text, tables = "", []
            if status == "readable":
                text, tables = page_text(page, number)
            assessed.append((number, status, reasons, flags, text, tables))
        b.metadata = _metadata(pdf.metadata, len(assessed))

    if ocr_page is not None:
        unreadable_count = sum(1 for _, status, *_ in assessed if status == "unreadable")
        if unreadable_count > limits.max_ocr_pages:
            raise LimitExceeded(
                "too_many_ocr_pages",
                f"PDF has {unreadable_count} unreadable pages, exceeding max_ocr_pages"
                f" ({limits.max_ocr_pages})",
                actual=unreadable_count,
                limit=limits.max_ocr_pages,
            )

    for number, status, reasons, flags, text, tables in assessed:
        confidence: float | None = None
        if status == "unreadable" and ocr_page is not None:
            text, confidence, reason, ocr_flags = ocr_page(number)
            if reason is None:
                status = "readable"
                flags += ["ocr", *ocr_flags]
            else:
                reasons.append(reason)
                text, confidence = "", None
        if status == "readable":
            b.block("page", text, page=number, flags=flags, confidence=confidence)
            for rows in tables:
                b.table(rows, page=number)
        else:
            b.block("page", "", page=number, status=status, reasons=reasons)
        pages[number] = (status, reasons, flags, confidence)
    _issues(b, pages)
    return b.build()


# A gap of at least this fraction of the font size separates two words. pdfplumber's
# default is an absolute 3 points, which merges words in PDFs that draw no space glyphs
# and leave a quarter-em gap instead (Google Docs via Skia), and splits letter-spaced
# titles. Measured on 32 PDFs: 0.2 to 0.22 fix both without touching the other files;
# 0.24 begins to split words in tables.
X_TOLERANCE_RATIO = 0.22
TABLE_SETTINGS = {"text_x_tolerance_ratio": X_TOLERANCE_RATIO}


def _plumber_page_text(page, _number):
    text = CID.sub("\ufffd", page.extract_text(x_tolerance_ratio=X_TOLERANCE_RATIO) or "")
    return text, page.extract_tables(TABLE_SETTINGS)


def parse_pdfplumber(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
    return _parse(path, _plumber_page_text, limits=limits)


def _clean_pdfium(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\ufffe", "-")  # PDFium marks hyphens FFFE
    return "".join(
        "\ufffd" if unicodedata.category(ch) == "Cc" and ch not in "\t\n" else ch for ch in text
    ).strip()


# PDFium is not thread-safe, and parse/probe share one process-wide lock: every PDFium
# call goes through `_PDFIUM`. Pages and text pages are closed explicitly inside it, so
# no garbage-collection finalizer calls into PDFium later from another thread.
_PDFIUM = PDFIUM_LOCK


def parse_pdfium(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
    check_file_bytes(path, limits)
    check_pdf_page_count(path, limits.max_pdf_pages)
    with _PDFIUM:
        doc = pypdfium2.PdfDocument(path)
    try:

        def page_text(_page, number):
            with _PDFIUM:
                page = doc[number - 1]
                textpage = page.get_textpage()
                text = textpage.get_text_range()
                textpage.close()
                page.close()
            return _clean_pdfium(text), []

        return _parse(path, page_text, limits=limits)
    finally:
        with _PDFIUM:
            doc.close()
