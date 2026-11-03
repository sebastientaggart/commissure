"""XLSX, CSV, and JSON parsers.

XLSX cells are read as stored values (cached results for formulas), not as Excel
displays them: no number formats. A formula with no cached value (a file written by a
tool that never calculated it) becomes an empty cell and an `uncached_formulas` issue,
never a guessed value. Trailing empty rows and columns are trimmed per sheet.

CSV uses the probe's encoding and delimiter detection and normalizes rows to
tab-separated text. JSON text is the value pretty-printed, keys included.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.cell.read_only import EMPTY_CELL
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from commissure.limits import (
    DEFAULT_LIMITS,
    LimitExceeded,
    Limits,
    check_archive_bytes,
    check_file_bytes,
)
from commissure.parse.model import DocBuilder, ParsedDoc, rows_to_text
from commissure.probe.tabular import detect_delimiter, detect_encoding

SNIFF_BYTES = 64 * 1024
LIST_FIRST = 20
# Spreadsheets routinely use a handful of blank rows to separate title blocks, sub-tables,
# and footnotes on one sheet; preserving gaps up to 32 rows keeps layout exact while
# collapsing accidental million-row voids (such as a stray paste at row 1,048,576) to one
# blank separator row with an explicit `sparse_rows_collapsed` warning issue.
MAX_EMPTY_ROW_GAP = 32


def _value(v: Any) -> None | bool | int | float | str:
    """Normalize a cell value to an RFC 8259 JSON primitive matching `rows_to_text`.

    `extracted_text.structure` enforces SQLite `CHECK (json_valid(structure))`, which
    rejects non-JSON objects (`timedelta` from duration formats, `Decimal`, rich text,
    unevaluated formula objects) and non-finite `NaN`/`Infinity` floats.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return float(v) if math.isfinite(v) else str(v)
    if isinstance(v, dt.datetime | dt.date | dt.time):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, ArrayFormula | DataTableFormula):
        return None
    if isinstance(v, bytes | bytearray | memoryview):
        s = bytes(v).decode("utf-8", errors="replace")
        return s if s.strip() else None
    s = v if type(v) is str else str(v)
    return s if s.strip() else None


def _sheet_rows(
    ws: Any,
    wf: Any,
    uncached: list[str],
    collapsed_gaps: list[dict[str, Any]],
    *,
    limits: Limits,
    state: dict[str, int],
) -> list[list[Any]]:
    """Cached values from `ws`; `wf` is the same sheet read for formulas.

    `openpyxl`'s `ReadOnlyWorksheet` yields the same `EMPTY_CELL` tuple reference across
    missing XML `<row>` ranges, so pointer identity skips million-row voids in O(1) per row.
    """
    retained: list[list[Any]] = []
    max_col_used = 0
    empty_gap = 0
    gap_start_row = 0
    seen_empty_v: Any = None
    seen_empty_f: Any = None

    for r, (vrow, frow) in enumerate(zip(ws.iter_rows(), wf.iter_rows(), strict=True), 1):
        v_fast_empty = vrow is seen_empty_v or not vrow
        if not v_fast_empty and vrow[0] is EMPTY_CELL and all(c is EMPTY_CELL for c in vrow):
            seen_empty_v = vrow
            v_fast_empty = True
        f_fast_empty = frow is seen_empty_f or not frow
        if not f_fast_empty and frow[0] is EMPTY_CELL and all(c is EMPTY_CELL for c in frow):
            seen_empty_f = frow
            f_fast_empty = True
        if v_fast_empty and f_fast_empty:
            if empty_gap == 0:
                gap_start_row = r
            empty_gap += 1
            continue

        vals: list[Any] = []
        last_used = 0
        for c, (vcell, fcell) in enumerate(zip(vrow, frow, strict=True), 1):
            if getattr(fcell, "data_type", None) == "f" and vcell.value is None:
                uncached.append(f"{ws.title}!{get_column_letter(c)}{r}")
            val = _value(vcell.value)
            vals.append(val)
            if val is not None:
                last_used = c

        if last_used == 0:
            if empty_gap == 0:
                gap_start_row = r
            empty_gap += 1
            continue

        if empty_gap > 0:
            if empty_gap <= MAX_EMPTY_ROW_GAP:
                retained.extend([] for _ in range(empty_gap))
            else:
                keep_sep = 1 if retained else 0
                collapsed = empty_gap - keep_sep
                if keep_sep:
                    retained.append([])
                collapsed_gaps.append(
                    {
                        "sheet": ws.title,
                        "from_row": gap_start_row,
                        "to_row": r - 1,
                        "collapsed_rows": collapsed,
                    }
                )
            empty_gap = 0

        state["nonempty_rows"] += 1
        if state["nonempty_rows"] > limits.max_table_rows:
            raise LimitExceeded(
                "too_many_rows",
                f"workbook exceeds max_table_rows ({limits.max_table_rows})",
                actual=state["nonempty_rows"],
                limit=limits.max_table_rows,
            )
        max_col_used = max(max_col_used, last_used)
        retained.append(vals[:last_used])
        total_cells = state["prior_cells"] + len(retained) * max_col_used
        if total_cells > limits.max_table_cells:
            raise LimitExceeded(
                "too_many_cells",
                f"workbook exceeds max_table_cells ({limits.max_table_cells})",
                actual=total_cells,
                limit=limits.max_table_cells,
            )

    if not retained or max_col_used == 0:
        return []
    state["prior_cells"] += len(retained) * max_col_used
    return [row + [None] * (max_col_used - len(row)) for row in retained]


def parse_xlsx(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
    check_file_bytes(path, limits)
    check_archive_bytes(path, limits)
    b = DocBuilder()
    uncached: list[str] = []
    collapsed_gaps: list[dict[str, Any]] = []
    sheets = []
    state = {"nonempty_rows": 0, "prior_cells": 0}
    # File objects, not paths: blobs have no extension and openpyxl checks it.
    with path.open("rb") as f_values, path.open("rb") as f_formulas:
        values = load_workbook(f_values, read_only=True, data_only=True)
        formulas = load_workbook(f_formulas, read_only=True, data_only=False)
        try:
            if len(values.sheetnames) > limits.max_sheets:
                raise LimitExceeded(
                    "too_many_sheets",
                    f"workbook has {len(values.sheetnames)} sheets, exceeding max_sheets"
                    f" ({limits.max_sheets})",
                    actual=len(values.sheetnames),
                    limit=limits.max_sheets,
                )
            for ws, wf in zip(values.worksheets, formulas.worksheets, strict=True):
                rows = _sheet_rows(ws, wf, uncached, collapsed_gaps, limits=limits, state=state)
                index = b.table(rows, sheet=ws.title)
                hidden = [] if ws.sheet_state == "visible" else [ws.sheet_state]
                b.block("sheet", rows_to_text(rows), name=ws.title, table=index, flags=hidden)
                sheets.append({"name": ws.title, "state": ws.sheet_state, "rows": len(rows)})
        finally:
            values.close()
            formulas.close()
    b.metadata = {"sheets": sheets}
    if uncached:
        b.issue("uncached_formulas", count=len(uncached), first_cells=uncached[:LIST_FIRST])
    if collapsed_gaps:
        b.issue(
            "sparse_rows_collapsed",
            total_collapsed_rows=sum(g["collapsed_rows"] for g in collapsed_gaps),
            gaps=collapsed_gaps[:LIST_FIRST],
        )
    return b.build()


def parse_csv(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
    check_file_bytes(path, limits)
    b = DocBuilder()
    encoding, flags = detect_encoding(path)
    errors = "replace" if encoding == "cp1252" else "strict"
    rows: list[list[Any]] = []
    nonempty_rows = 0
    total_cells = 0
    with path.open(encoding=encoding, errors=errors, newline="") as f:
        delimiter = detect_delimiter(f.read(SNIFF_BYTES)) or ","
        f.seek(0)
        for raw_row in csv.reader(f, delimiter=delimiter):
            total_cells += len(raw_row)
            if total_cells > limits.max_table_cells:
                raise LimitExceeded(
                    "too_many_cells",
                    f"CSV exceeds max_table_cells ({limits.max_table_cells})",
                    actual=total_cells,
                    limit=limits.max_table_cells,
                )
            norm = [_value(v) for v in raw_row]
            if any(v is not None for v in norm):
                nonempty_rows += 1
                if nonempty_rows > limits.max_table_rows:
                    raise LimitExceeded(
                        "too_many_rows",
                        f"CSV exceeds max_table_rows ({limits.max_table_rows})",
                        actual=nonempty_rows,
                        limit=limits.max_table_rows,
                    )
            rows.append(norm)
    for flag in flags:
        b.issue(flag)
    width = len(rows[0]) if rows else 0
    ragged = [i + 1 for i, r in enumerate(rows) if r and len(r) != width]
    if ragged:
        b.issue("ragged_rows", count=len(ragged), first_rows=ragged[:LIST_FIRST])
    index = b.table(rows)
    b.block("table", rows_to_text(rows), table=index)
    b.metadata = {"encoding": encoding, "delimiter": delimiter, "rows": len(rows)}
    return b.build()


def parse_json(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
    check_file_bytes(path, limits)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    b = DocBuilder()
    b.block("document", json.dumps(data, indent=2, ensure_ascii=False))
    b.metadata = {"top_level": type(data).__name__}
    return b.build()
