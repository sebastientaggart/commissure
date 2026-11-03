"""CSV and XLSX probes: per-sheet schema, null rates, and sample rows.

The header is assumed to be the first non-empty row, and the output says so. Fully
empty rows are counted separately and excluded from null rates. Spreadsheet formulas
are reported as `formula`, not evaluated: cached values are not read, so a formula
column is never mistaken for a null one. Numbers are one type (`number`); openpyxl
turns integral floats into ints, so an int/float split would be noise.
"""

from __future__ import annotations

import codecs
import csv
import datetime as dt
from collections import Counter
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.cell.read_only import EMPTY_CELL
from openpyxl.utils import get_column_letter

from commissure.limits import (
    DEFAULT_LIMITS,
    LimitExceeded,
    Limits,
    check_archive_bytes,
    check_file_bytes,
)

HEADER_ASSUMPTION = "first non-empty row"
SNIFF_BYTES = 64 * 1024
SNIFF_ROWS = 50
DELIMITERS = (",", ";", "\t", "|")


def _json_value(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    return value


def _schema_from_nonempty(
    total_rows: int,
    nonempty: list[tuple[int, list[tuple[str, Any]]]],
    sample_rows: int,
) -> dict[str, Any]:
    """Shared shape for one table from `(0-based row index, typed cells)` of non-empty rows."""
    if not nonempty:
        return {
            "header_row": None,
            "data_rows": 0,
            "empty_rows": total_rows,
            "columns": [],
            "sample_rows": [],
            "flags": ["no_content"],
        }
    header_index, header = nonempty[0]
    width = max(
        max((j + 1 for j, (k, _) in enumerate(r) if k != "empty"), default=0) for _, r in nonempty
    )
    data = [r for _, r in nonempty[1:]]
    flags = []
    if sum(k != "empty" for k, _ in header) == 1 and width > 1:
        flags.append("possible_title_row")

    columns = []
    for j in range(width):
        kinds = Counter(r[j][0] if j < len(r) else "empty" for r in data)
        nulls = kinds.pop("empty", 0)
        name = header[j][1] if j < len(header) and header[j][0] != "empty" else None
        columns.append(
            {
                "index": j + 1,
                "letter": get_column_letter(j + 1),
                "header": _json_value(name),
                "types": dict(sorted(kinds.items())),
                "nulls": nulls,
                "null_rate": round(nulls / len(data), 4) if data else None,
            }
        )
    if any("formula" in c["types"] for c in columns):
        flags.append("has_formulas")
    return {
        "header_row": header_index + 1,
        "header_assumption": HEADER_ASSUMPTION,
        "data_rows": len(data),
        "empty_rows": total_rows - len(nonempty),
        "columns": columns,
        "sample_rows": [[_json_value(v) for _, v in r[:width]] for r in data[:sample_rows]],
        "flags": flags,
    }


def _schema(rows: list[list[tuple[str, Any]]], sample_rows: int) -> dict[str, Any]:
    """Shared shape for one table of typed cells: (kind, value) per cell."""
    nonempty = [(i, r) for i, r in enumerate(rows) if any(k != "empty" for k, _ in r)]
    return _schema_from_nonempty(len(rows), nonempty, sample_rows)


def _xlsx_cell(cell: Any) -> tuple[str, Any]:
    value, kind = cell.value, getattr(cell, "data_type", None)
    if value is None or (isinstance(value, str) and not value.strip()):
        return "empty", None
    if kind == "f":
        return "formula", value
    if kind == "e":
        return "error", value
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return "date", value
    if isinstance(value, int | float):
        return "number", value
    return "string", value


def probe_xlsx(path: Path, sample_rows: int, *, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    check_file_bytes(path, limits)
    check_archive_bytes(path, limits)
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        if len(wb.sheetnames) > limits.max_sheets:
            raise LimitExceeded(
                "too_many_sheets",
                f"workbook has {len(wb.sheetnames)} sheets, exceeding max_sheets"
                f" ({limits.max_sheets})",
                actual=len(wb.sheetnames),
                limit=limits.max_sheets,
            )
        sheets = []
        total_nonempty = 0
        prior_cells = 0
        for ws in wb.worksheets:
            total_rows = 0
            sheet_width = 0
            nonempty: list[tuple[int, list[tuple[str, Any]]]] = []
            seen_empty: Any = None
            for idx, row in enumerate(ws.iter_rows()):
                total_rows += 1
                if row is seen_empty or not row:
                    continue
                if row[0] is EMPTY_CELL and all(c is EMPTY_CELL for c in row):
                    seen_empty = row
                    continue
                typed = [_xlsx_cell(c) for c in row]
                last_used = max(
                    (j + 1 for j, (k, _) in enumerate(typed) if k != "empty"), default=0
                )
                if last_used == 0:
                    continue
                total_nonempty += 1
                if total_nonempty > limits.max_table_rows:
                    raise LimitExceeded(
                        "too_many_rows",
                        f"workbook exceeds max_table_rows ({limits.max_table_rows})",
                        actual=total_nonempty,
                        limit=limits.max_table_rows,
                    )
                sheet_width = max(sheet_width, last_used)
                nonempty.append((idx, typed[:last_used]))
                sheet_cells = prior_cells + len(nonempty) * sheet_width
                if sheet_cells > limits.max_table_cells:
                    raise LimitExceeded(
                        "too_many_cells",
                        f"workbook exceeds max_table_cells ({limits.max_table_cells})",
                        actual=sheet_cells,
                        limit=limits.max_table_cells,
                    )
            padded = [(i, r + [("empty", None)] * (sheet_width - len(r))) for i, r in nonempty]
            prior_cells += len(padded) * sheet_width
            sheets.append(
                {
                    "name": ws.title,
                    "state": ws.sheet_state,
                    "rows": total_rows,
                    **_schema_from_nonempty(total_rows, padded, sample_rows),
                }
            )
    finally:
        wb.close()
    return {"sheets": sheets}


def _csv_cell(value: str) -> tuple[str, Any]:
    if not value.strip():
        return "empty", None
    try:
        float(value)
    except ValueError:
        return "string", value
    return "number", value


def detect_encoding(path: Path) -> tuple[str, list[str]]:
    with path.open("rb") as f:
        head = f.read(4)
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", []
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", []
    try:
        with path.open(encoding="utf-8") as f:
            while f.read(1 << 20):
                pass
    except UnicodeDecodeError:
        return "cp1252", ["not_utf8_decoded_as_cp1252"]
    return "utf-8", []


def detect_delimiter(sample: str) -> str | None:
    """The candidate under which the most leading rows share one field count above 1.

    Ties go to the earlier candidate in DELIMITERS. Ragged rows lower a candidate's
    score but do not disqualify it, unlike csv.Sniffer.
    """
    lines = sample.splitlines(keepends=True)[:-1] or sample.splitlines(keepends=True)
    best, best_score = None, 0
    for delimiter in DELIMITERS:
        rows = [r for r in csv.reader(lines[:SNIFF_ROWS], delimiter=delimiter) if r]
        counts = Counter(len(r) for r in rows)
        width, score = max(counts.items(), key=lambda kv: (kv[1], kv[0]), default=(0, 0))
        if width > 1 and score > best_score:
            best, best_score = delimiter, score
    return best


def probe_csv(path: Path, sample_rows: int, *, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    check_file_bytes(path, limits)
    encoding, flags = detect_encoding(path)
    errors = "replace" if encoding == "cp1252" else "strict"
    total_rows = 0
    total_cells = 0
    row_lengths: list[int] = []
    nonempty: list[tuple[int, list[tuple[str, Any]]]] = []
    with path.open(encoding=encoding, errors=errors, newline="") as f:
        delimiter = detect_delimiter(f.read(SNIFF_BYTES))
        if delimiter is None:
            delimiter = ","
            flags.append("single_column_or_no_delimiter")
        f.seek(0)
        for idx, r in enumerate(csv.reader(f, delimiter=delimiter)):
            total_rows += 1
            row_lengths.append(len(r))
            total_cells += len(r)
            if total_cells > limits.max_table_cells:
                raise LimitExceeded(
                    "too_many_cells",
                    f"CSV exceeds max_table_cells ({limits.max_table_cells})",
                    actual=total_cells,
                    limit=limits.max_table_cells,
                )
            typed = [_csv_cell(v) for v in r]
            if any(k != "empty" for k, _ in typed):
                nonempty.append((idx, typed))
                if len(nonempty) > limits.max_table_rows:
                    raise LimitExceeded(
                        "too_many_rows",
                        f"CSV exceeds max_table_rows ({limits.max_table_rows})",
                        actual=len(nonempty),
                        limit=limits.max_table_rows,
                    )
    schema = _schema_from_nonempty(total_rows, nonempty, sample_rows)
    header_len = row_lengths[schema["header_row"] - 1] if schema["header_row"] else 0
    ragged = [
        i + 1
        for i, length in enumerate(row_lengths)
        if schema["header_row"]
        and i >= schema["header_row"]
        and length > 0
        and length != header_len
    ]
    if ragged:
        flags.append("ragged_rows")
    schema["flags"] = flags + schema["flags"]
    return {
        "encoding": encoding,
        "delimiter": delimiter,
        "rows": total_rows,
        "ragged_rows": len(ragged),
        "ragged_row_numbers_first_20": ragged[:20],
        **schema,
    }
