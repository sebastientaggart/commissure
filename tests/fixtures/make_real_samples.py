"""Generate tests/fixtures/real_samples/.

Run: uv run python tests/fixtures/make_real_samples.py

Builds a compact, deterministic, non-government corpus exercising real-world document
quirks covered by `tests/test_real_samples.py`, `tests/test_parse_golden.py`,
`tests/test_parse.py`, and `tests/test_ocr.py`:
- `project_report.pdf`: 2-page born-digital PDF with a 24pt letter-spaced title
  (`0.22 em` font-relative word gap), a line-wrapped hyphenated word (`minority-owned`,
  which PDFium marks with U+FFFE), and a ruled 3x3 table on page 1.
- `cover_unmapped.pdf`: 1-page report cover over a full-page background image whose
  font ToUnicode CMap omits glyph 0x0001 (`unmapped_glyphs` + `full_page_image_under_text`,
  replaced with U+FFFD by both `pdfplumber` and `pdfium`).
- `type3_unnamed.pdf`: 2-page PDF whose Type 3 font uses numeric `/Differences` glyph
  names (`/0`, `/1`, `/2`, ...) and no `/ToUnicode`. Rejected by the structural gate
  (`font_without_unicode_map`) and recognised by `parse --parser ocr` via PDFium + Tesseract.
- `template.docx`: Word template with H1-H4 headings, 2 tables, and bold form prompts
  (`possible_unstyled_headings`).
- `inventory.xlsx`: 2-sheet workbook (`Field Descriptions` with a single-cell title row
  flagged `possible_title_row`, and `Inventory` with sparse nulls and trailing empty rows).
- `events.json`: 10-element top-level JSON array (`top_level == "list"`).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from docx import Document
from make_probe_corpus import LETTER, Pdf, esc, full_page, lines, text_block
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "real_samples"


def _hex2(text: str, unmapped_at: str = "\x01") -> str:
    """Encode ASCII text as 2-byte big-endian CIDs, mapping `unmapped_at` to CID 0x0001."""
    out = []
    for ch in text:
        cid = 1 if ch == unmapped_at else ord(ch)
        out.append(f"{cid:04X}")
    return "<" + "".join(out) + ">"


def project_report_pdf(path: Path) -> None:
    """2-page born-digital report: letter-spaced title, wrapped hyphen, and ruled table."""
    pdf = Pdf()
    f = {"F1": pdf.helvetica}

    # 1. Letter-spaced 24pt title: -145 thousandths of an em adds 3.48pt between letters
    # (> pdfplumber's 3pt default, < 0.22 * 24 = 5.28pt font-relative threshold).
    p1 = (
        "BT /F1 24 Tf 72 720 Td "
        "[(P) -145 (R) -145 (O) -145 (J) -145 (E) -145 (C) -145 (T) "
        "-380 (R) -145 (E) -145 (P) -145 (O) -145 (R) -145 (T)] TJ ET\n"
    )
    # 2. Body lines with a soft hyphen (0xAD, octal \255) at a line wrap ("minority\255" / "owned"),
    # which PDFium extracts as U+FFFE before `parse_pdfium` normalizes it to "minority-owned".
    body = [
        "Quarterly operational review for regional service centers and partner clinics.",
        "Programs operated by minority\xad",
        "owned and community-based organizations reported steady intake volume",
        "across all four quarters of the current review period.",
        *lines(8),
    ]
    p1 += text_block(72, 670, body, size=11)

    # 3. Ruled 3x3 table at y = 380..470, x = 72..432 (3 columns of width 120, 3 rows of height 30).
    grid = []
    for y in (380, 410, 440, 470):
        grid.append(f"72 {y} m 432 {y} l S\n")
    for x in (72, 192, 312, 432):
        grid.append(f"{x} 380 m {x} 470 l S\n")
    rows = [
        ("Workstream", "Planned", "Completed"),
        ("Intake", "120", "118"),
        ("Review", "95", "95"),
    ]
    cells = []
    for r_idx, row in enumerate(rows):
        y = 450 - r_idx * 30
        for c_idx, val in enumerate(row):
            x = 82 + c_idx * 120
            cells.append(f"BT /F1 10 Tf {x} {y} Td ({esc(val)}) Tj ET\n")
    p1 += "0.5 w\n" + "".join(grid) + "".join(cells)
    pdf.page(p1, fonts=f)

    # Page 2: clean narrative text.
    pdf.page(text_block(72, 720, lines(24), size=11), fonts=f)
    pdf.write(path)


def cover_unmapped_pdf(path: Path) -> None:
    """1-page report cover over a full-page image where CID 1 has no ToUnicode entry."""
    pdf = Pdf()
    bg = pdf.image(120, 160, background=240, bands=False)

    # Build a Type0 / CIDFontType2 font with a ToUnicode CMap covering 0x0020..0x007E,
    # deliberately omitting CID 0x0001.
    cmap_lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /PartialUTF16 def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
        "1 beginbfrange",
        "<0020> <007E> <0020>",
        "endbfrange",
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    to_uni = pdf.stream("", "\n".join(cmap_lines).encode("ascii"))
    desc = pdf.add(
        b"<< /Type /FontDescriptor /FontName /PartialSans /Flags 32"
        b" /FontBBox [0 -200 1000 800] /ItalicAngle 0 /Ascent 800 /Descent -200"
        b" /CapHeight 700 /StemV 80 >>"
    )
    cid = pdf.add(
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Helvetica"
        b" /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
        + f" /FontDescriptor {desc} 0 R /DW 550 >>".encode()
    )
    f_partial = pdf.add(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /Helvetica"
        + f" /Encoding /Identity-H /DescendantFonts [{cid} 0 R] /ToUnicode {to_uni} 0 R >>".encode()
    )

    text_rows = [
        "Annual Operations Plan",
        "Department\x01of Operations and Field Services",
        *lines(28),
    ]
    ops = [full_page(LETTER), "BT /F2 11 Tf 14 TL 72 700 Td\n"]
    for i, r in enumerate(text_rows):
        prefix = "" if i == 0 else "T* "
        ops.append(f"{prefix}{_hex2(r)} Tj\n")
    ops.append("ET\n")
    pdf.page("".join(ops), fonts={"F2": f_partial}, images={"Im1": bg})
    pdf.write(path)


def _bitmap_charproc(ch: str, font: ImageFont.ImageFont) -> tuple[int, bytes]:
    """Render `ch` with Pillow and convert ink pixel runs into Type 3 `re f` rectangles."""
    if ch == " ":
        return 300, b"300 0 0 0 0 0 d1\n"
    # Draw white ink (255) on black background (0) so getbbox() measures the ink bounds.
    canvas = Image.new("L", (64, 64), 0)
    draw = ImageDraw.Draw(canvas)
    draw.text((2, 2), ch, font=font, fill=255)
    bbox = canvas.getbbox()
    if not bbox:
        return 300, b"300 0 0 0 0 0 d1\n"
    w_px = min(64, max(8, bbox[2] + 3))
    scale = 20  # 1 pixel -> 20 glyph units (height ~ 800 units)
    wx = w_px * scale
    rects = [f"{wx} 0 0 -200 {wx} 800 d1"]
    px = canvas.load()
    for y_px in range(64):
        x0 = None
        for x_px in range(w_px):
            ink = px[x_px, y_px] > 96
            if ink and x0 is None:
                x0 = x_px
            elif not ink and x0 is not None:
                gy = (42 - y_px) * scale
                rects.append(f"{x0 * scale} {gy} {(x_px - x0) * scale} {scale} re f")
                x0 = None
        if x0 is not None:
            gy = (42 - y_px) * scale
            rects.append(f"{x0 * scale} {gy} {(w_px - x0) * scale} {scale} re f")
    return wx, ("\n".join(rects) + "\n").encode("ascii")


def type3_unnamed_pdf(path: Path) -> None:
    """2-page PDF whose Type 3 font uses numeric glyph names (/0, /1, ...) and no ToUnicode,
    yet draws real letterforms so PDFium + Tesseract OCR can read both pages."""
    pdf = Pdf()
    pil_font = ImageFont.load_default(size=36)
    p1_lines = [
        "MEMORANDUM 2024-10",
        "December 13, 2024",
        "Subject: Quarterly Field Operations Review",
        "All regional offices completed the standard intake checklist.",
    ]
    p2_lines = [
        "Section 2: Summary of Findings",
        "Every reviewed batch met the completeness threshold.",
    ]
    charset = sorted(set("".join(p1_lines + p2_lines)))
    code_of = {ch: idx + 32 for idx, ch in enumerate(charset)}

    diffs = ["32"]
    procs = []
    widths = []
    for idx, ch in enumerate(charset):
        gname = f"/{idx}"  # numeric glyph names ("0", "1", ...): none in Adobe Glyph List
        wx, stream_bytes = _bitmap_charproc(ch, pil_font)
        proc_id = pdf.stream("", stream_bytes)
        diffs.append(gname)
        procs.append(f"{gname} {proc_id} 0 R")
        widths.append(str(wx))

    enc_id = pdf.add(f"<< /Type /Encoding /Differences [{' '.join(diffs)}] >>".encode())
    cp_id = pdf.add(f"<< {' '.join(procs)} >>".encode())
    f_type3 = pdf.add(
        b"<< /Type /Font /Subtype /Type3 /FontBBox [0 -200 1000 800]"
        b" /FontMatrix [0.001 0 0 0.001 0 0]"
        + f" /CharProcs {cp_id} 0 R /Encoding {enc_id} 0 R"
        f" /FirstChar 32 /LastChar {31 + len(charset)} /Widths [{' '.join(widths)}] >>".encode()
    )

    for page_lines in (p1_lines, p2_lines):
        cmds = []
        y = 680
        for line in page_lines:
            hex_str = "".join(f"{code_of[ch]:02X}" for ch in line)
            cmds.append(f"BT /F1 18 Tf 54 {y} Td <{hex_str}> Tj ET\n")
            y -= 42
        pdf.page("".join(cmds), fonts={"F1": f_type3})
    pdf.write(path)


def template_docx(path: Path) -> None:
    """DOCX with nested headings (H1-H4), 2 tables, and bold Normal-style form prompts."""
    doc = Document()
    doc.core_properties.title = "Program Improvement Plan Template"
    doc.core_properties.author = "Operations Team"
    doc.core_properties.created = dt.datetime(2026, 1, 15, tzinfo=dt.UTC)
    doc.core_properties.modified = dt.datetime(2026, 1, 15, tzinfo=dt.UTC)

    doc.add_heading("Program Improvement Plan", level=1)
    doc.add_paragraph("Complete each section below and attach supporting schedules.")
    doc.add_heading("Part 1: Baseline Assessment", level=2)
    doc.add_paragraph("Summarize the findings from the prior review cycle.")
    doc.add_heading("Section 1.1: Root Cause Analysis", level=3)
    doc.add_paragraph("Identify the operational bottlenecks contributing to delays.")
    doc.add_heading("Item 1.1.1: Intake Verification", level=4)
    doc.add_paragraph("Document the verification steps performed at intake.")

    for prompt in ("Grantee Organization Name", "Primary Contact Title"):
        p = doc.add_paragraph()
        p.add_run(prompt).bold = True

    for t_idx in range(1, 3):
        table = doc.add_table(rows=3, cols=3)
        for c, label in enumerate(("Milestone", "Owner", "Target Quarter")):
            table.cell(0, c).text = label
        table.cell(1, 0).text = f"Action Step {t_idx}.A"
        table.cell(1, 1).text = "Regional Lead"
        table.cell(1, 2).text = "Q2"
        table.cell(2, 0).text = f"Action Step {t_idx}.B"
        table.cell(2, 1).text = "Quality Analyst"
        table.cell(2, 2).text = "Q3"
    doc.save(path)


def inventory_xlsx(path: Path) -> None:
    """2-sheet workbook: Field Descriptions (title row in A1) and Inventory (table + nulls)."""
    wb = Workbook()
    wb.properties.created = dt.datetime(2026, 1, 15, tzinfo=dt.UTC)
    wb.properties.modified = dt.datetime(2026, 1, 15, tzinfo=dt.UTC)

    fields = wb.active
    fields.title = "Field Descriptions"
    fields.append(["Outcome Measure Inventory: Field Descriptions", None])
    fields.append(["Field Name", "Definition"])
    fields.append(["Measure ID", "Unique identifier assigned to the instrument."])
    fields.append(["Domain", "Primary operational domain assessed by the measure."])
    fields.append(["Validated", "Whether the instrument has completed validation."])

    inv = wb.create_sheet("Inventory")
    inv.append(["Measure ID", "Instrument Name", "Domain", "Validated", "Notes"])
    inv.append(["M-101", "Intake Completeness Index", "Intake", "Yes", None])
    inv.append(["M-102", "Turnaround Interval Tracker", "Processing", "Yes", "Core metric"])
    inv.append(["M-103", "Reviewer Agreement Scale", "Quality", "No", None])
    inv.append([None, None, None, None, None])  # trailing blank row trimmed by parse_xlsx
    wb.save(path)


def events_json(path: Path) -> None:
    """10-record top-level JSON array."""
    records = [
        {
            "event_id": f"EVT-{1000 + i}",
            "region": f"Region {(i % 4) + 1}",
            "week": i + 1,
            "count": 12 + i * 3,
        }
        for i in range(10)
    ]
    path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    project_report_pdf(OUT / "project_report.pdf")
    cover_unmapped_pdf(OUT / "cover_unmapped.pdf")
    type3_unnamed_pdf(OUT / "type3_unnamed.pdf")
    template_docx(OUT / "template.docx")
    inventory_xlsx(OUT / "inventory.xlsx")
    events_json(OUT / "events.json")
    for p in sorted(OUT.iterdir()):
        if p.is_file():
            print(f"wrote {p.name} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
