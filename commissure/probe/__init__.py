"""Reconnaissance over a file or folder. Reads files directly; needs no workspace and
writes nothing.

Files are found the same way ingest finds items (`walk_folder`). The file type is
taken from the extension; a file whose content does not match its extension is
reported as an error for that file, and probing continues.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from commissure.ingest import DEFAULT_IGNORE, walk_folder
from commissure.limits import DEFAULT_LIMITS, LimitExceeded, Limits
from commissure.probe.office import probe_docx
from commissure.probe.pdf import probe_pdf
from commissure.probe.tabular import probe_csv, probe_xlsx

INSPECTED = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".csv": "csv"}


def _inspect(kind: str, path: Path, sample_rows: int, limits: Limits) -> dict[str, Any]:
    if kind == "pdf":
        return probe_pdf(path, limits=limits)
    if kind == "docx":
        return probe_docx(path, limits=limits)
    if kind == "xlsx":
        return probe_xlsx(path, sample_rows, limits=limits)
    return probe_csv(path, sample_rows, limits=limits)


def probe_path(
    path: Path,
    sample_rows: int = 3,
    *,
    ignore: Sequence[str] = DEFAULT_IGNORE,
    limits: Limits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    path = path.resolve()
    if path.is_dir():
        files, stat_errors, ignored = walk_folder(path, ignore, max_files=limits.max_source_files)
        total_bytes = sum(sz for _, _, sz in files)
        if total_bytes > limits.max_source_bytes:
            raise LimitExceeded(
                "source_too_large",
                f"source size {total_bytes} bytes exceeds max_source_bytes"
                f" ({limits.max_source_bytes})",
                actual=total_bytes,
                limit=limits.max_source_bytes,
            )
    elif path.is_file():
        files = [(path.name, path, path.stat().st_size)]
        stat_errors = []
        ignored = 0
    else:
        raise ValueError(f"{path} does not exist")

    by_extension: Counter[str] = Counter()
    entries = []
    for locator, file, size_bytes in files:
        ext = file.suffix.lower() or "(none)"
        by_extension[ext] += 1
        entry: dict[str, Any] = {
            "locator": locator,
            "extension": ext,
            "size_bytes": size_bytes,
        }
        kind = INSPECTED.get(ext)
        if kind is None:
            entry["status"] = "not_inspected"
        else:
            try:
                entry[kind] = _inspect(kind, file, sample_rows, limits)
                entry["status"] = "inspected"
            except LimitExceeded as exc:
                entry["status"] = "error"
                entry["error"] = f"{exc.code}: {exc}"
            except Exception as exc:  # one bad file never stops the probe
                entry["status"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"
        entries.append(entry)
    for locator, _, message in stat_errors:
        ext = Path(locator).suffix.lower() or "(none)"
        by_extension[ext] += 1
        entries.append(
            {
                "locator": locator,
                "extension": ext,
                "size_bytes": None,
                "status": "error",
                "error": message,
            }
        )
    entries.sort(key=lambda e: e["locator"])

    status = Counter(e["status"] for e in entries)
    pdfs = [e["pdf"] for e in entries if "pdf" in e]
    summary = {
        "files": len(entries),
        "ignored": ignored,
        "inspected": status["inspected"],
        "not_inspected": status["not_inspected"],
        "errors": status["error"],
        "pdf_pages": sum(p["pages"] for p in pdfs),
        "readable_pages": sum(p["readable"] for p in pdfs),
        "unreadable_pages": sum(p["unreadable"] for p in pdfs),
        "blank_pages": sum(p["blank"] for p in pdfs),
    }
    return {
        "path": str(path),
        "by_extension": dict(sorted(by_extension.items())),
        "summary": summary,
        "files": entries,
    }


def probe_failed(report: dict[str, Any]) -> bool:
    """Zero successes is a failure: nothing found, or every inspectable file errored."""
    s = report["summary"]
    return s["files"] == 0 or (s["errors"] > 0 and s["inspected"] == 0)
