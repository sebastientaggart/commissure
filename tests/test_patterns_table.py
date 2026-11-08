"""Unit tests for OutputFile, verify_output_file, and write_verified_csv in patterns/table.py."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from commissure.patterns import OutputFile, verify_output_file, write_verified_csv


def test_verify_output_file_checks_existence_size_bounds_and_hash(tmp_path: Path) -> None:
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    nested = outputs_root / "sub" / "report.html"
    nested.parent.mkdir(parents=True)
    payload = b"<!doctype html><title>Report</title>\n"
    nested.write_bytes(payload)

    verified = verify_output_file(nested, outputs_root)
    assert verified == OutputFile(
        path="sub/report.html",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        row_count=None,
    )

    with pytest.raises(ValidationError):
        OutputFile(
            path="a.csv",
            sha256="0" * 64,
            size_bytes=10,
            unexpected_field=1,  # type: ignore[call-arg]
        )

    empty = outputs_root / "empty.html"
    empty.write_bytes(b"")
    with pytest.raises(RuntimeError, match="missing or empty"):
        verify_output_file(empty, outputs_root)

    with pytest.raises(RuntimeError, match="missing or empty"):
        verify_output_file(outputs_root / "missing.html", outputs_root)

    outside = tmp_path / "outside.html"
    outside.write_bytes(b"data")
    with pytest.raises(ValueError, match="outside outputs_root"):
        verify_output_file(outside, outputs_root)

    with pytest.raises(ValueError, match="non-negative"):
        verify_output_file(nested, outputs_root, row_count=-1)


def test_write_verified_csv_writes_and_verifies_rows_including_multiline_cells(
    tmp_path: Path,
) -> None:
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    csv_path = outputs_root / "tables" / "items.csv"

    headers = ["id", "title", "notes"]
    rows = [
        ["doc-1", "First item", "Line one\nLine two, with comma"],
        ["doc-2", "Second item", "Clean cell"],
    ]
    out = write_verified_csv(csv_path, outputs_root, headers, rows)
    assert out.path == "tables/items.csv"
    assert out.row_count == 2
    assert out.size_bytes == csv_path.stat().st_size > 0
    assert out.sha256 == hashlib.sha256(csv_path.read_bytes()).hexdigest()

    with csv_path.open(encoding="utf-8", newline="") as f:
        parsed = list(csv.reader(f))
    assert parsed == [headers, *rows]

    empty_csv = outputs_root / "empty.csv"
    empty_out = write_verified_csv(empty_csv, outputs_root, ["col_a", "col_b"], [])
    assert empty_out.path == "empty.csv"
    assert empty_out.row_count == 0
    assert empty_out.size_bytes > 0

    with pytest.raises(ValueError, match="headers must not be empty"):
        write_verified_csv(outputs_root / "bad.csv", outputs_root, [], [])

    with pytest.raises(ValueError, match="expected 2 headers"):
        write_verified_csv(outputs_root / "bad.csv", outputs_root, ["a", "b"], [["only_one"]])

    with pytest.raises(ValueError, match="outside outputs_root"):
        write_verified_csv(tmp_path / "outside.csv", outputs_root, ["a"], [["1"]])
