"""write_record_table: structural checks, empty state, fail-closed external URLs."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from commissure.api import page_external_resource_count
from commissure.patterns import Column, write_record_table

COLUMNS = [
    Column(key="locator", header="Locator"),
    Column(key="page", header="Page"),
    Column(key="via_ocr", header="Via OCR"),
]


def test_writes_csv_and_html_with_matching_rows(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    records = [
        {"locator": "a/item.pdf", "page": 2, "via_ocr": False},
        {"locator": "b/scan.pdf", "page": 1, "via_ocr": True},
    ]
    written = write_record_table(
        records, COLUMNS, dest, stem="items", title="Items", subtitle="Test table"
    )
    assert written.row_count == 2
    assert written.csv_path.is_file() and written.csv_path.stat().st_size > 0
    assert written.html_path.is_file() and written.html_path.stat().st_size > 0

    with written.csv_path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["Locator", "Page", "Via OCR"]
    assert rows[1] == ["a/item.pdf", "2", "false"]
    assert rows[2] == ["b/scan.pdf", "1", "true"]
    assert len(rows) - 1 == len(records)

    html = written.html_path.read_text(encoding="utf-8")
    assert "Locator" in html and "Page" in html and "Via OCR" in html
    assert "2 rows" in html
    assert "a/item.pdf" in html and "b/scan.pdf" in html
    assert 'href="static/pico.min.css"' in html
    assert "https://" not in html or "cdn." not in html
    assert page_external_resource_count(html) == 0
    assert (dest / "static" / "pico.min.css").is_file()
    assert (dest / "static" / "report.js").is_file()


def test_empty_input_writes_honest_empty_page(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    written = write_record_table([], COLUMNS, dest, stem="empty", title="Empty")
    assert written.row_count == 0
    with written.csv_path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows == [["Locator", "Page", "Via OCR"]]
    html = written.html_path.read_text(encoding="utf-8")
    assert "0 rows" in html
    assert "No rows." in html
    assert "Locator" in html
    # Header only: no fabricated sample body row with data cells beyond thead.
    assert html.count("<tbody>") == 1
    assert "<tbody>" in html and "</tbody>" in html
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert "<tr>" not in body


def test_missing_key_is_empty_cell_and_model_dump_works(tmp_path: Path) -> None:
    class Row(BaseModel):
        model_config = ConfigDict(extra="forbid")
        locator: str
        via_ocr: bool

    dest = tmp_path / "out"
    dest.mkdir()
    written = write_record_table(
        [Row(locator="x.docx", via_ocr=False), {"locator": "y.pdf"}],
        COLUMNS,
        dest,
        stem="partial",
        title="Partial",
    )
    with written.csv_path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[1] == ["x.docx", "", "false"]
    assert rows[2] == ["y.pdf", "", ""]


def test_list_cell_raises_and_writes_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(ValueError, match="list"):
        write_record_table(
            [{"locator": "a", "page": [1, 2], "via_ocr": False}],
            COLUMNS,
            dest,
            stem="bad",
            title="Bad",
        )
    assert not (dest / "bad.csv").exists()
    assert not (dest / "bad.html").exists()
    assert not (dest / "static").exists()


def test_external_url_in_brand_css_is_refused(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    brand = tmp_path / "brand"
    brand.mkdir()
    (brand / "brand.css").write_text(
        "body { background: url(https://cdn.example/a.css); }\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="external resource"):
        write_record_table(
            [{"locator": "a", "page": 1, "via_ocr": False}],
            COLUMNS,
            dest,
            stem="cdn",
            title="CDN",
            brand_dir=brand,
        )
    assert not (dest / "cdn.csv").exists()
    assert not (dest / "cdn.html").exists()
    assert not (dest / "static").exists()


def test_script_tag_in_cell_is_escaped_not_a_resource(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    payload = '<script src="https://cdn.example/x.js"></script>'
    written = write_record_table(
        [{"locator": payload, "page": 1, "via_ocr": False}],
        COLUMNS,
        dest,
        stem="escape",
        title="Escape",
    )
    html = written.html_path.read_text(encoding="utf-8")
    assert '<script src="https://cdn.example/x.js">' not in html
    assert "&lt;script" in html
    assert page_external_resource_count(html) == 0
    # A bare https citation in cell text is not a loaded resource.
    cite = write_record_table(
        [{"locator": "see https://example.gov/doc", "page": 1, "via_ocr": False}],
        COLUMNS,
        dest,
        stem="cite",
        title="Cite",
    )
    cite_html = cite.html_path.read_text(encoding="utf-8")
    assert "https://example.gov/doc" in cite_html
    assert page_external_resource_count(cite_html) == 0


def test_stem_with_separator_and_empty_columns_raise(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(ValueError, match="stem"):
        write_record_table([], COLUMNS, dest, stem="../x", title="T")
    with pytest.raises(ValueError, match="columns"):
        write_record_table([], [], dest, stem="x", title="T")
