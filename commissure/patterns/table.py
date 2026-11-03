"""Write a flat record table as CSV and an offline HTML page.

The page is a fragment wrapped by ``render_page``. Columns are declared by
the caller. Missing values are empty cells; there is no sample row and no
plausible default. A non-zero ``page_external_resource_count`` refuses both files.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from commissure.deliver.html import page_external_resource_count, render_page

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_TEMPLATE = "record_table.html.j2"
_CELL_TYPES = (str, int, float, bool, type(None))


@dataclass(frozen=True)
class Column:
    key: str
    header: str


@dataclass(frozen=True)
class WrittenTable:
    csv_path: Path
    html_path: Path
    row_count: int


def _as_mapping(record: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    dump = getattr(record, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, Mapping):
            return data
    raise TypeError(f"record must be a mapping or have model_dump(), got {type(record)!r}")


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ValueError(
        f"cell value must be str, int, float, bool, or None; got {type(value).__name__}"
    )


def _rows(
    records: Sequence[Mapping[str, Any] | Any],
    columns: Sequence[Column],
) -> list[list[str]]:
    rows: list[list[str]] = []
    for record in records:
        data = _as_mapping(record)
        row: list[str] = []
        for col in columns:
            value = data.get(col.key)
            if value is not None and not isinstance(value, _CELL_TYPES):
                raise ValueError(
                    f"column {col.key!r}: cell value must be str, int, float, bool, or None;"
                    f" got {type(value).__name__}"
                )
            row.append(_cell_text(value))
        rows.append(row)
    return rows


def write_record_table(
    records: Sequence[Mapping[str, Any] | Any],
    columns: Sequence[Column],
    dest: Path,
    *,
    stem: str,
    title: str,
    subtitle: str = "",
    brand_dir: Path | None = None,
) -> WrittenTable:
    """Write ``stem.csv`` and ``stem.html`` into ``dest`` through the shared page shell."""
    if Path(stem).name != stem or not stem or "/" in stem or "\\" in stem:
        raise ValueError(f"stem must be a single file name, not {stem!r}")
    if not columns:
        raise ValueError("columns must not be empty")
    if not dest.is_dir():
        raise FileNotFoundError(f"destination directory {dest} does not exist")

    rows = _rows(records, columns)
    headers = [c.header for c in columns]
    context = {
        "title": title,
        "headers": headers,
        "rows": rows,
        "row_count": len(rows),
        "is_empty": not rows,
    }
    preview = render_page(
        template_dir=_TEMPLATE_DIR,
        template_name=_TEMPLATE,
        context=context,
        title=title,
        subtitle=subtitle,
        brand_dir=brand_dir,
    )
    hits = page_external_resource_count(preview, brand_dir)
    if hits:
        raise ValueError(f"refusing to write table: {hits} external resource URL(s)")

    # Assets copy only after the scan; write the data files only after that succeeds.
    html = render_page(
        template_dir=_TEMPLATE_DIR,
        template_name=_TEMPLATE,
        context=context,
        title=title,
        subtitle=subtitle,
        brand_dir=brand_dir,
        outputs_dir=dest,
    )
    csv_path = dest / f"{stem}.csv"
    html_path = dest / f"{stem}.html"
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    csv_path.write_text(buf.getvalue(), encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")

    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        raise RuntimeError(f"output {csv_path.name} is missing or empty")
    if not html_path.is_file() or html_path.stat().st_size == 0:
        raise RuntimeError(f"output {html_path.name} is missing or empty")
    data_rows = sum(1 for _ in csv.reader(csv_path.open(encoding="utf-8"))) - 1
    if data_rows != len(records):
        raise RuntimeError(
            f"CSV row count {data_rows} does not match {len(records)} input record(s)"
        )
    return WrittenTable(csv_path=csv_path, html_path=html_path, row_count=len(records))
