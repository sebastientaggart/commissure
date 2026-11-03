"""DOCX parser: body paragraphs, headings, and tables in reading order.

Headings use the probe's rule (`heading_level`). Each paragraph and table records the
heading path above it. Table text is tab-separated rows; a cell covered by a merge is
None, so rows stay rectangular and merged text appears once. Like the probe, only
top-level body content is read: no text boxes, headers, footers, footnotes, or tracked
changes.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.table import Table

from commissure.limits import (
    DEFAULT_LIMITS,
    LimitExceeded,
    Limits,
    check_archive_bytes,
    check_file_bytes,
)
from commissure.parse.model import DocBuilder, ParsedDoc, rows_to_text
from commissure.probe.office import heading_level


def _table_rows(table: Table) -> list[list[str | None]]:
    grid = [row.cells for row in table.rows]  # python-docx rebuilds the grid per call
    rows = []
    for r, cells in enumerate(grid):
        values = []
        for c, cell in enumerate(cells):
            covered = (c > 0 and cell._tc is cells[c - 1]._tc) or (
                r > 0 and c < len(grid[r - 1]) and cell._tc is grid[r - 1][c]._tc
            )  # horizontal span repeats the cell to the left, vertical the one above
            values.append(None if covered else cell.text)
        rows.append(values)
    return rows


def parse_docx(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
    check_file_bytes(path, limits)
    check_archive_bytes(path, limits)
    doc = Document(str(path))
    b = DocBuilder()
    props = doc.core_properties
    b.metadata = {
        k: (v.isoformat() if hasattr(v, "isoformat") else v)
        for k, v in {
            "title": props.title,
            "subject": props.subject,
            "author": props.author,
            "created": props.created,
            "modified": props.modified,
        }.items()
        if v
    }
    path_stack: list[tuple[int, str]] = []
    total_rows = 0
    total_cells = 0
    for block in doc.iter_inner_content():
        headings = [text for _, text in path_stack]
        if isinstance(block, Table):
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
            rows = _table_rows(block)
            index = b.table(rows, heading_path=headings)
            b.block("table", rows_to_text(rows), table=index, heading_path=headings)
            continue
        text = block.text.strip()
        if not text:
            continue
        level = heading_level(block)
        if level is None:
            b.block("paragraph", text, heading_path=headings)
            continue
        while path_stack and path_stack[-1][0] >= level:
            path_stack.pop()
        b.block("heading", text, level=level, heading_path=[t for _, t in path_stack])
        path_stack.append((level, text))
    return b.build()
