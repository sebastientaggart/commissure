"""Generate tests/fixtures/probe_corpus/. Run: uv run python tests/fixtures/make_probe_corpus.py

The PDFs are written by hand, object by object, so each page has exactly the structure
the readability gate must judge. Every page's purpose is noted where it is built. The
outputs are checked in; rerun this only to change the corpus, and review the diff.
"""

from __future__ import annotations

import csv
import datetime as dt
import zlib
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from openpyxl import Workbook

OUT = Path(__file__).parent / "probe_corpus"
LETTER, A4 = (612, 792), (595, 842)

WORDS = [
    "the",
    "committee",
    "reviewed",
    "each",
    "section",
    "of",
    "the",
    "draft",
    "and",
    "noted",
    "where",
    "figures",
    "were",
    "missing",
    "or",
    "inconsistent",
    "with",
    "the",
    "prior",
    "period",
    "so",
    "that",
    "owners",
    "could",
    "respond",
    "before",
    "the",
    "next",
    "review",
    "cycle",
    "closed",
]


def lines(n: int, width: int = 80) -> list[str]:
    out, cur, i = [], "", 0
    while len(out) < n:
        word = WORDS[i % len(WORDS)]
        i += 1
        if len(cur) + len(word) + 1 > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return out


def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


# --- minimal PDF writer -------------------------------------------------------------


class Pdf:
    def __init__(self) -> None:
        self.objects: list[bytes] = []
        self.pages: list[int] = []
        self.helvetica = self.add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        )
        self._garbled: int | None = None

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def stream(self, header: str, data: bytes) -> int:
        return self.add(
            f"<< {header} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"
        )

    def garbled_font(self) -> int:
        """A composite font with Identity-H encoding and no ToUnicode: glyph ids only."""
        if self._garbled is None:
            desc = self.add(
                b"<< /Type /FontDescriptor /FontName /GlyphOnlySans /Flags 4"
                b" /FontBBox [0 -200 1000 800] /ItalicAngle 0 /Ascent 800 /Descent -200"
                b" /CapHeight 700 /StemV 80 >>"
            )
            cid = self.add(
                b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /GlyphOnlySans"
                b" /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
                + f" /FontDescriptor {desc} 0 R /DW 600 >>".encode()
            )
            self._garbled = self.add(
                b"<< /Type /Font /Subtype /Type0 /BaseFont /GlyphOnlySans"
                + f" /Encoding /Identity-H /DescendantFonts [{cid} 0 R] >>".encode()
            )
        return self._garbled

    def image(self, width: int, height: int, background: int = 255, bands: bool = True) -> int:
        """Grayscale image; with `bands`, dark horizontal strokes like lines of a scan."""
        rows = []
        for y in range(height):
            row = bytearray([background] * width)
            if bands and (y // 4) % 3 == 0 and height * 0.08 < y < height * 0.92:
                for x in range(int(width * 0.1), int(width * 0.9)):
                    if (x // 7) % 5:
                        row[x] = 40
            rows.append(bytes(row))
        data = zlib.compress(b"".join(rows))
        return self.stream(
            f"/Type /XObject /Subtype /Image /Width {width} /Height {height}"
            " /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode",
            data,
        )

    def page(self, content: str, size=LETTER, fonts=None, images=None) -> None:
        res = []
        if fonts:
            res.append("/Font << " + " ".join(f"/{k} {v} 0 R" for k, v in fonts.items()) + " >>")
        if images:
            res.append(
                "/XObject << " + " ".join(f"/{k} {v} 0 R" for k, v in images.items()) + " >>"
            )
        contents = self.stream("", content.encode("latin-1"))
        self.pages.append(
            self.add(
                f"<< /Type /Page /Parent PAGES /MediaBox [0 0 {size[0]} {size[1]}]"
                f" /Resources << {' '.join(res)} >> /Contents {contents} 0 R >>".encode()
            )
        )

    def write(self, path: Path) -> None:
        pages_id = len(self.objects) + 1
        catalog_id = pages_id + 1
        kids = " ".join(f"{p} 0 R" for p in self.pages)
        objects = [o.replace(b"PAGES", f"{pages_id} 0 R".encode()) for o in self.objects]
        objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>".encode())
        objects.append(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
        path.write_bytes(bytes(out))


def text_block(x: int, y: int, rows: list[str], size: int = 11, mode: int = 0) -> str:
    body = " T* ".join(f"({esc(r)}) Tj" for r in rows)
    return f"BT /F1 {size} Tf {mode} Tr {size + 3} TL {x} {y} Td {body} ET\n"


def full_page(size) -> str:
    return f"q {size[0]} 0 0 {size[1]} 0 0 cm /Im1 Do Q\n"


# --- PDFs ---------------------------------------------------------------------------


def native_pdf(path: Path) -> None:
    """Every page is native and readable. Pages 2-4 trap gates that judge by text amount
    or by image area alone."""
    pdf = Pdf()
    f = {"F1": pdf.helvetica}
    # 1: an ordinary dense text page.
    pdf.page(text_block(72, 720, lines(44)), fonts=f)
    # 2: a sparse title page, only one word. Little text is not the same as scanned.
    pdf.page(text_block(260, 420, ["Appendix"], size=24), fonts=f)
    # 3: a large photo with a two-line caption (image covers about 60% of the page).
    photo = pdf.image(240, 180, background=150, bands=False)
    pdf.page(
        "q 460 0 0 630 76 140 cm /Im1 Do Q\n"
        + text_block(
            76,
            110,
            ["Figure 1. Site photo taken during the review visit.", "Source: field team."],
            size=10,
        ),
        fonts=f,
        images={"Im1": photo},
    )
    # 4: a full-page letterhead background under dense native text.
    letterhead = pdf.image(120, 160, background=245, bands=False)
    pdf.page(
        full_page(LETTER) + text_block(72, 700, lines(40)), fonts=f, images={"Im1": letterhead}
    )
    pdf.write(path)


def scanned_pdf(path: Path) -> None:
    """Image-only pages, as a scanner produces: no text layer at all."""
    pdf = Pdf()
    pdf.page(full_page(LETTER), images={"Im1": pdf.image(306, 396)})
    pdf.page(full_page(A4), size=A4, images={"Im1": pdf.image(298, 421)})
    pdf.write(path)


def mixed_pdf(path: Path) -> None:
    """One page per gate outcome, in a single file."""
    pdf = Pdf()
    f = {"F1": pdf.helvetica}
    scan = pdf.image(306, 396)
    # 1: native text -> readable.
    pdf.page(text_block(72, 720, lines(30)), fonts=f)
    # 2: a scan with a native ID stamp added on top -> unreadable (image dominant).
    pdf.page(
        full_page(LETTER) + text_block(450, 30, ["ID-000123"], size=9),
        fonts=f,
        images={"Im1": scan},
    )
    # 3: text in a font with no unicode mapping -> unreadable (undecodable glyphs).
    glyphs = "".join(f"{g:04X}" for g in range(36, 96))
    content = "".join(f"BT /F2 11 Tf 72 {720 - 14 * i} Td <{glyphs}> Tj ET\n" for i in range(20))
    pdf.page(content, fonts={"F2": pdf.garbled_font()})
    # 4: a scan with an invisible OCR text layer (render mode 3) -> readable, flagged.
    pdf.page(
        full_page(LETTER) + text_block(72, 720, lines(44), mode=3), fonts=f, images={"Im1": scan}
    )
    # 5: blank.
    pdf.page("")
    # 6: vector drawing only (a bar chart) and no text -> unreadable (no text layer).
    bars = "".join(f"{100 + 80 * i} 200 50 {60 * (i + 1)} re f\n" for i in range(5))
    pdf.page("0.2 g\n" + bars + "1 w 80 190 m 540 190 l S\n")
    pdf.write(path)


def type3_pdf(path: Path) -> None:
    """Three Type 3 font cases (PDF 1.7 §9.6.5, no /BaseFont):
    1. /Encoding /Differences with Adobe Glyph List names -> readable.
    2. No /Encoding and no /ToUnicode -> unreadable (undecodable_glyphs + font_without_unicode_map).
    3. /Encoding /Differences with custom /gN names and no /ToUnicode -> unreadable
       (undecodable_glyphs: structurally encoded, but glyph names do not map to Unicode).
    """
    pdf = Pdf()
    glyph_box = pdf.stream("", b"500 0 40 0 460 680 d1\n40 0 420 680 re f\n")
    space_box = pdf.stream("", b"250 0 0 0 0 0 d1\n")

    sentence = "Type three font with standard glyph names."
    agl = {" ": "space", ".": "period"}
    for ch in sorted(set(sentence) - set(agl)):
        agl[ch] = ch
    diffs = " ".join(f"{ord(ch)} /{name}" for ch, name in sorted(agl.items()))
    procs = " ".join(
        f"/{name} {space_box if ch == ' ' else glyph_box} 0 R" for ch, name in sorted(agl.items())
    )
    widths = " ".join("250" if c == 32 else "500" for c in range(32, 123))
    enc_agl = pdf.add(f"<< /Type /Encoding /Differences [{diffs}] >>".encode())
    cp_agl = pdf.add(f"<< {procs} >>".encode())
    f_encoded = pdf.add(
        b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 500 700]"
        b" /FontMatrix [0.001 0 0 0.001 0 0]"
        + f" /CharProcs {cp_agl} 0 R /Encoding {enc_agl} 0 R"
        f" /FirstChar 32 /LastChar 122 /Widths [{widths}] >>".encode()
    )

    custom_names = [f"g{i}" for i in range(1, 15)]
    cp_custom = pdf.add(
        ("<< " + " ".join(f"/{n} {glyph_box} 0 R" for n in custom_names) + " >>").encode()
    )
    widths_custom = " ".join(["500"] * len(custom_names))
    f_no_enc = pdf.add(
        b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 500 700]"
        b" /FontMatrix [0.001 0 0 0.001 0 0]"
        + f" /CharProcs {cp_custom} 0 R"
        f" /FirstChar 1 /LastChar {len(custom_names)} /Widths [{widths_custom}] >>".encode()
    )
    enc_custom = pdf.add(
        (
            "<< /Type /Encoding /Differences [1 " + " ".join(f"/{n}" for n in custom_names) + "] >>"
        ).encode()
    )
    f_custom = pdf.add(
        b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 500 700]"
        b" /FontMatrix [0.001 0 0 0.001 0 0]"
        + f" /CharProcs {cp_custom} 0 R /Encoding {enc_custom} 0 R"
        f" /FirstChar 1 /LastChar {len(custom_names)} /Widths [{widths_custom}] >>".encode()
    )

    hex_codes = "".join(f"{i:02X}" for i in range(1, len(custom_names) + 1))
    pdf.page(f"BT /F1 12 Tf 72 700 Td ({esc(sentence)}) Tj ET\n", fonts={"F1": f_encoded})
    pdf.page(f"BT /F2 12 Tf 72 700 Td <{hex_codes}> Tj ET\n", fonts={"F2": f_no_enc})
    pdf.page(f"BT /F3 12 Tf 72 700 Td <{hex_codes}> Tj ET\n", fonts={"F3": f_custom})
    pdf.write(path)


# --- office files -------------------------------------------------------------------


def report_docx(path: Path) -> None:
    doc = Document()
    doc.core_properties.created = dt.datetime(2026, 1, 1)
    doc.core_properties.modified = dt.datetime(2026, 1, 1)
    doc.add_heading("Overview", level=1)
    doc.add_paragraph(" ".join(lines(3)))
    doc.add_heading("Scope", level=2)
    doc.add_paragraph(" ".join(lines(2)))
    table = doc.add_table(rows=3, cols=4)
    for c, label in enumerate(["Area", "Owner", "Status", "Due"]):
        table.cell(0, c).text = label
    table.cell(1, 0).text = "Intake"
    table.cell(2, 0).merge(table.cell(2, 1)).text = "Merged across two columns"
    doc.add_heading("Details", level=3)
    doc.add_paragraph(" ".join(lines(2)))
    doc.add_heading("Results", level=1)
    doc.add_heading("Skipped a level", level=3)  # H1 -> H3
    fake = doc.add_paragraph()
    fake.add_run("Looks Like A Heading").bold = True  # Normal style, all bold
    doc.add_paragraph(" ".join(lines(2)))
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    doc.add_table(rows=2, cols=2)
    doc.save(path)


def workbook_xlsx(path: Path) -> None:
    wb = Workbook()
    wb.properties.created = dt.datetime(2026, 1, 1)
    wb.properties.modified = dt.datetime(2026, 1, 1)
    data = wb.active
    data.title = "Data"
    data.append(["id", "name", "amount", "date", "double"])
    rows = [
        (1, "alpha", 10.5, dt.date(2026, 1, 5)),
        (2, None, 20.0, dt.date(2026, 2, 5)),
        (3, "gamma", None, None),
        (4, "delta", 7.25, dt.date(2026, 4, 5)),
    ]
    for i, row in enumerate(rows, start=2):
        data.append([*row, f"=C{i}*2"])
    notes = wb.create_sheet("Notes")
    notes.append(["Quarterly notes"])  # a title row above the real header
    notes.append([])
    notes.append(["key", "value"])
    notes.append(["owner", "ops"])
    notes.append(["status", None])
    wb.save(path)


def table_csv(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["code", "label", "qty"])
        w.writerow(["A1", "first", "3"])
        w.writerow(["A2", "", "4.5"])
        w.writerow(["A3", "third"])  # ragged: one field short
        w.writerow(["A4", "fourth", ""])


def quoted_bom_csv(path: Path) -> None:
    """UTF-8 BOM + comma-delimited export where row 3 has unescaped inner quotes around a
    string containing a comma, so csv.reader exits quote mode early and splits the field."""
    raw = (
        "\ufeffgrantee_id,organization,state,award\r\n"
        'HHS-001,"Bright Start Family Services",MD,250000\r\n'
        'HHS-002,"St. Jude "Mercy, West" Clinic",PA,180000\r\n'
        'HHS-003,"Harbor Community Health",VA,310000\r\n'
        'HHS-004,"Mountain View, Inc.",WV,145000\r\n'
    )
    path.write_bytes(raw.encode("utf-8"))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    native_pdf(OUT / "native.pdf")
    scanned_pdf(OUT / "scanned.pdf")
    mixed_pdf(OUT / "mixed.pdf")
    type3_pdf(OUT / "type3.pdf")
    (OUT / "broken.pdf").write_bytes((OUT / "native.pdf").read_bytes()[:300])
    report_docx(OUT / "report.docx")
    workbook_xlsx(OUT / "workbook.xlsx")
    table_csv(OUT / "table.csv")
    quoted_bom_csv(OUT / "quoted_bom.csv")
    (OUT / "notes.txt").write_text("Counted by extension, not inspected.\n")


if __name__ == "__main__":
    main()
