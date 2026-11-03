"""DOCX probe: heading and table topology of the body, in reading order.

A heading is a paragraph with an outline level, set on the paragraph or inherited
through its style chain (Word's built-in heading styles carry one), or a style named
`Heading N`. The `Title` style is level 0. Only top-level body content is walked;
tables nested inside table cells are not counted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from commissure.limits import (
    DEFAULT_LIMITS,
    LimitExceeded,
    Limits,
    check_archive_bytes,
    check_file_bytes,
)

UNSTYLED_HEADING_MAX_CHARS = 120


def _outline_level(ppr: Any) -> int | None:
    if ppr is None:
        return None
    el = ppr.find(qn("w:outlineLvl"))
    if el is None:
        return None
    val = int(el.get(qn("w:val")))
    return None if val >= 9 else val + 1  # 9 is "body text"


def heading_level(par: Paragraph) -> int | None:
    level = _outline_level(par._p.pPr)
    style = par.style
    while level is None and style is not None:
        level = _outline_level(style.element.pPr)
        style = style.base_style
    if level is not None:
        return level
    name = par.style.name if par.style is not None else ""
    if name == "Title":
        return 0
    m = re.fullmatch(r"Heading (\d)", name or "")
    return int(m.group(1)) if m else None


def _starts_merge(tc: Any) -> bool:
    """True for the first cell of a merged region (horizontal, vertical, or both)."""
    tcpr = tc.tcPr
    if tcpr is None:
        return False
    vmerge = tcpr.find(qn("w:vMerge"))
    if vmerge is not None and vmerge.get(qn("w:val")) != "restart":
        return False  # continuation of a vertical merge started above
    span = tcpr.find(qn("w:gridSpan"))
    return vmerge is not None or (span is not None and int(span.get(qn("w:val"))) > 1)


def _looks_like_heading(par: Paragraph) -> bool:
    text = par.text.strip()
    runs = [r for r in par.runs if r.text.strip()]
    return bool(runs) and len(text) <= UNSTYLED_HEADING_MAX_CHARS and all(r.bold for r in runs)


def probe_docx(path: Path, *, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    check_file_bytes(path, limits)
    check_archive_bytes(path, limits)
    doc = Document(str(path))
    headings: list[dict] = []
    tables: list[dict] = []
    skips: list[dict] = []
    unstyled: list[str] = []
    paragraphs = 0
    total_rows = 0
    total_cells = 0
    current: str | None = None
    previous_level: int | None = None

    for block in doc.iter_inner_content():
        if isinstance(block, Table):
            tbl = block._tbl
            n_rows = len(block.rows)
            n_cols = len(block.columns)
            total_rows += n_rows
            if total_rows > limits.max_table_rows:
                raise LimitExceeded(
                    "too_many_rows",
                    f"DOCX tables exceed max_table_rows ({limits.max_table_rows})",
                    actual=total_rows,
                    limit=limits.max_table_rows,
                )
            total_cells += n_rows * n_cols
            if total_cells > limits.max_table_cells:
                raise LimitExceeded(
                    "too_many_cells",
                    f"DOCX tables exceed max_table_cells ({limits.max_table_cells})",
                    actual=total_cells,
                    limit=limits.max_table_cells,
                )
            merged = sum(_starts_merge(tc) for tc in tbl.iter(qn("w:tc")))
            tables.append(
                {
                    "index": len(tables),
                    "rows": n_rows,
                    "cols": n_cols,
                    "merged_regions": merged,
                    "under_heading": current,
                }
            )
            continue
        text = block.text.strip()
        level = heading_level(block)
        if level is not None and text:
            if previous_level is not None and level > previous_level + 1:
                skips.append({"from": previous_level, "to": level, "heading": text})
            headings.append({"level": level, "text": text})
            current, previous_level = text, level
        elif text:
            paragraphs += 1
            if _looks_like_heading(block):
                unstyled.append(text)

    return {
        "paragraphs": paragraphs,
        "headings": headings,
        "tables": tables,
        "flags": {"heading_level_skips": skips, "possible_unstyled_headings": unstyled},
    }
