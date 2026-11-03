"""Keyword and phrase search over current parsed text (FTS5).

Reads `extracted_text_fts` joined to `current_text`, so tombstoned and non-current
versions stay out. Hits point at a source location (locator, parser, page, offsets);
the snippet is display-only and is never an extraction input. Callers extract from
the item text using `start` and `end` as a pointer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from commissure.parse import PARSERS
from commissure.recipes import parsers_for

SNIPPET_CONTEXT = 80
DEFAULT_LIMIT = 20
DEFAULT_TEXT = ("ocr", "default")
# Sentinels for FTS5 highlight(); must not appear in extracted text. Spans come from
# highlight() because SQLite's FTS5 rejects offsets() here.
_HL_OPEN = "\x1e"
_HL_CLOSE = "\x1f"


class SearchUsageError(ValueError):
    """Empty query, unknown parser, bad limit, or FTS5 syntax error."""


class SearchFromWorker(RuntimeError):
    """`ctx.search` was called from a worker thread."""


@dataclass(frozen=True)
class Hit:
    locator: str
    source_id: int
    item_version_id: int
    parser: str
    page: int | None
    snippet: str
    start: int
    end: int
    via_ocr: bool
    confidence: float | None
    extracted_text_id: int
    stale: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def phrase_query(query: str) -> str:
    """One FTS5 phrase: quotes doubled, the whole string wrapped in quotes."""
    return '"' + query.replace('"', '""') + '"'


def block_at(structure: dict[str, Any], offset: int) -> dict[str, Any] | None:
    """The block where start <= offset < end. Empty spans (start == end) contain nothing."""
    for block in structure.get("blocks") or ():
        start = block.get("start")
        end = block.get("end")
        if start is None or end is None:
            continue
        if start <= offset < end:
            return block
    return None


def snippet(text: str, start: int, end: int, *, context: int = SNIPPET_CONTEXT) -> str:
    """Display window around `[start, end)`. The match is included in full; context is bounded."""
    left = max(0, start - context)
    right = min(len(text), end + context)
    body = text[left:right]
    collapsed = re.sub(r"\s+", " ", body).strip()
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{collapsed}{suffix}"


def _preferred_parser(locator: str, rows_by_parser: dict[str, sqlite3.Row]) -> str | None:
    preferred = parsers_for(DEFAULT_TEXT, locator)
    for name in preferred:
        row = rows_by_parser.get(name)
        if row is not None and not row["stale"]:
            return name
    for name in preferred:
        if name in rows_by_parser:
            return name
    return None


def _location(structure: dict[str, Any], start: int) -> tuple[int | None, bool, float | None]:
    block = block_at(structure, start)
    if block is None:
        return None, False, None
    flags = block.get("flags") or []
    return block.get("page"), "ocr" in flags, block.get("confidence")


def _spans_from_highlight(marked: str) -> list[tuple[int, int]]:
    """Character spans of highlighted terms, mapped back to the unmarked text."""
    spans: list[tuple[int, int]] = []
    i = 0
    orig = 0
    open_len = len(_HL_OPEN)
    close_len = len(_HL_CLOSE)
    while i < len(marked):
        if marked.startswith(_HL_OPEN, i):
            i += open_len
            start = orig
            j = marked.find(_HL_CLOSE, i)
            if j < 0:
                break
            term = marked[i:j]
            spans.append((start, start + len(term)))
            orig += len(term)
            i = j + close_len
        else:
            orig += 1
            i += 1
    return spans


def _hits_from_row(
    row: sqlite3.Row,
    *,
    phrase: bool,
    query: str,
    highlighted: str | None,
) -> list[Hit]:
    text = row["text"]
    structure = json.loads(row["structure"])
    if phrase:
        spans = [
            (m.start(), m.end()) for m in re.finditer(re.escape(query), text, flags=re.IGNORECASE)
        ]
    else:
        spans = _spans_from_highlight(highlighted or "")

    out: list[Hit] = []
    for start, end in spans:
        page, via_ocr, confidence = _location(structure, start)
        out.append(
            Hit(
                locator=row["locator"],
                source_id=row["source_id"],
                item_version_id=row["item_version_id"],
                parser=row["parser"],
                page=page,
                snippet=snippet(text, start, end),
                start=start,
                end=end,
                via_ocr=via_ocr,
                confidence=confidence,
                extracted_text_id=row["extracted_text_id"],
                stale=bool(row["stale"]),
            )
        )
    return out


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    parser: str | None = None,
    limit: int = DEFAULT_LIMIT,
    phrase: bool = True,
) -> list[Hit]:
    """Search current parsed text. Raises `SearchUsageError` on bad input or FTS syntax."""
    if not query or not query.strip():
        raise SearchUsageError("query must not be empty")
    if limit < 1:
        raise SearchUsageError("limit must be at least 1")
    if parser is not None and parser not in PARSERS:
        raise SearchUsageError(f"unknown parser {parser!r}")

    match = phrase_query(query) if phrase else query
    if phrase:
        select = (
            "SELECT ct.item_id, ct.source_id, ct.locator, ct.item_version_id,"
            " ct.extracted_text_id, ct.parser, ct.text, ct.structure, ct.stale"
        )
    else:
        select = (
            "SELECT ct.item_id, ct.source_id, ct.locator, ct.item_version_id,"
            " ct.extracted_text_id, ct.parser, ct.text, ct.structure, ct.stale,"
            " highlight(extracted_text_fts, 0, ?, ?) AS fts_highlight"
        )
    sql = (
        select + " FROM extracted_text_fts"
        " JOIN current_text ct ON ct.extracted_text_id = extracted_text_fts.rowid"
        " WHERE extracted_text_fts MATCH ?"
    )
    params: list[Any] = []
    if not phrase:
        params.extend([_HL_OPEN, _HL_CLOSE])
    params.append(match)
    if parser is not None:
        sql += " AND ct.parser = ?"
        params.append(parser)
    sql += " ORDER BY ct.extracted_text_id"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "fts5" in msg or "syntax" in msg or "match" in msg:
            raise SearchUsageError(f"invalid FTS query: {exc}") from exc
        raise

    if parser is None:
        by_item: dict[int, dict[str, sqlite3.Row]] = {}
        for row in rows:
            by_item.setdefault(row["item_id"], {})[row["parser"]] = row
        chosen: list[sqlite3.Row] = []
        for item_id, by_parser in by_item.items():
            # Keep the preferred parser's MATCH row only. If that parser exists in
            # current_text but did not MATCH, the item is absent.
            locator = next(iter(by_parser.values()))["locator"]
            pref = _preferred_parser(
                locator,
                {
                    r["parser"]: r
                    for r in conn.execute(
                        "SELECT * FROM current_text WHERE item_id = ?", (item_id,)
                    )
                },
            )
            if pref is not None and pref in by_parser:
                chosen.append(by_parser[pref])
        rows = chosen

    rows = sorted(rows, key=lambda r: r["extracted_text_id"])
    hits: list[Hit] = []
    for row in rows:
        highlighted = None if phrase else row["fts_highlight"]
        row_hits = _hits_from_row(row, phrase=phrase, query=query, highlighted=highlighted)
        row_hits.sort(key=lambda h: h.start)
        for hit in row_hits:
            hits.append(hit)
            if len(hits) >= limit:
                return hits
    return hits
