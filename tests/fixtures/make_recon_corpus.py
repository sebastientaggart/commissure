"""Writes tests/fixtures/recon_corpus/. Run: uv run python tests/fixtures/make_recon_corpus.py

Small files that exercise `commissure recon`: a repeated line across pages, identical
text in two folders, a sheet first row, a broken PDF, and an unparsed extension.
The OCR scan is copied into the test's tmp_path from ocr_corpus/, not stored here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).parent
OUT = HERE / "recon_corpus"

_spec = importlib.util.spec_from_file_location("make_probe_corpus", HERE / "make_probe_corpus.py")
assert _spec is not None and _spec.loader is not None
_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_probe)
Pdf = _probe.Pdf
text_block = _probe.text_block


# Avoid the substring "PAGES": Pdf.write substitutes that token for the pages object id.
REPEATED = "REPEATED LINE ON EVERY PAGE"


def repeat_pdf(path: Path) -> None:
    """Three pages; each carries REPEATED LINE ON EVERY PAGE plus a page-only line."""
    pdf = Pdf()
    f = {"F1": pdf.helvetica}
    for n, unique in enumerate(("PAGE ONE ONLY", "PAGE TWO ONLY", "PAGE THREE ONLY"), start=1):
        pdf.page(
            text_block(72, 720, [f"Page {n}", REPEATED, unique]),
            fonts=f,
        )
    pdf.write(path)


def twin_pdf(path: Path) -> None:
    """One page of identical body text for text_hash grouping."""
    pdf = Pdf()
    f = {"F1": pdf.helvetica}
    pdf.page(text_block(72, 720, ["IDENTICAL BODY FOR HASH", "Shared twin content."]), fonts=f)
    pdf.write(path)


def sheet_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Data"
    ws.append(["Alpha", "Beta"])
    ws.append([1, 2])
    wb.save(path)


def main() -> None:
    if OUT.exists():
        for child in OUT.rglob("*"):
            if child.is_file():
                child.unlink()
        for child in sorted(OUT.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
    for name in ("alpha", "beta", "gamma"):
        (OUT / name).mkdir(parents=True, exist_ok=True)
    repeat_pdf(OUT / "alpha" / "repeat.pdf")
    twin_pdf(OUT / "alpha" / "twin.pdf")
    twin_pdf(OUT / "beta" / "twin.pdf")
    sheet_xlsx(OUT / "beta" / "sheet.xlsx")
    (OUT / "gamma" / "broken.pdf").write_bytes(b"not a pdf")
    (OUT / "loose.txt").write_text("plain text, no parser\n")


if __name__ == "__main__":
    main()
