"""PDF probe and the per-page readability gate.

The gate judges each page from its structure, never from how much text came out:

- glyph decodability: whether each font's glyphs map to real characters. pdfminer emits
  `(cid:N)` for a glyph it cannot map; U+FFFD, private-use, and control characters
  count as undecodable too. A font is broken on a page when most of its distinct glyphs
  there are undecodable; one unmapped glyph id (often the space) repeated many times
  is a local defect, flagged rather than failed.
- font encoding: whether the fonts drawing the glyphs carry a unicode mapping at all
  (a ToUnicode CMap, or an encoding that implies one).
- text-to-image ratio: glyph-box area relative to image area, which separates a scan
  carrying a stamped page number from a scan carrying a full OCR text layer.

`judge` is a pure function of `PageSignals` so later stages reuse the same gate. The
thresholds were tuned on the synthetic corpus plus a handful of real samples; revisit
them if a real page is misjudged.

Blind spots, because none of them is visible in structure: glyphs that decode to the
wrong characters (a ToUnicode map that lies, a shifted encoding), the quality of an OCR
text layer (flagged, never verified), and text render mode (invisible text is judged
like visible text).
"""

from __future__ import annotations

import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber
import pypdfium2
from pdfminer.encodingdb import name2unicode
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import PDFStream, resolve1
from pdfminer.psparser import PSLiteral
from pdfplumber.page import Page
from pdfplumber.utils.exceptions import PdfminerException

from commissure.limits import DEFAULT_LIMITS, LimitExceeded, Limits, check_file_bytes

# PDFium is not thread-safe; every PDFium call across probe and parse shares this lock.
PDFIUM_LOCK = threading.Lock()

FULL_PAGE_IMAGE = 0.85  # image coverage at or above this means the page is an image
MIN_DECODABLE = 0.95  # share of glyphs that must come from fonts that are not broken
BROKEN_FONT_DISTINCT = 0.5  # a font is broken when more of its distinct glyphs fail
MIN_TEXT_TO_IMAGE = 0.02  # glyph area / image area below this over a full-page image
UNMAPPED_FONT_SHARE = 0.5  # share of glyphs drawn in fonts without a unicode mapping
# A Type 3 /Differences encoding implies characters only when its glyph names do. Re-distilled
# Type 3 fonts name glyphs `0`, `1`, `i255`: none resolve, and pdfminer decodes codes 32 and
# up as ASCII punctuation, so the broken-font rule alone passes such pages as readable.
MIN_NAMED_GLYPHS = 0.5

STANDARD_14 = {
    "Courier",
    "Courier-Bold",
    "Courier-Oblique",
    "Courier-BoldOblique",
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-Oblique",
    "Helvetica-BoldOblique",
    "Times-Roman",
    "Times-Bold",
    "Times-Italic",
    "Times-BoldItalic",
    "Symbol",
    "ZapfDingbats",
}
SYMBOLIC_FLAG = 1 << 2


@dataclass(frozen=True)
class PageSignals:
    chars: int
    undecodable_chars: int
    broken_font_chars: int  # glyphs drawn by fonts broken on this page
    unmapped_font_chars: int
    image_coverage: float
    text_coverage: float
    vector_objects: int
    unmapped_fonts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    verdict: str  # readable | unreadable | blank
    reasons: list[str] = field(default_factory=list)  # why unreadable
    flags: list[str] = field(default_factory=list)  # readable, but worth a look


def judge(s: PageSignals) -> Verdict:
    if s.chars == 0:
        if s.image_coverage == 0 and s.vector_objects == 0:
            return Verdict("blank")
        if s.image_coverage >= FULL_PAGE_IMAGE:
            return Verdict("unreadable", ["image_only"])
        return Verdict("unreadable", ["no_text_layer"])

    reasons = []
    if (s.chars - s.broken_font_chars) / s.chars < MIN_DECODABLE:
        reasons.append("undecodable_glyphs")
    if s.unmapped_font_chars / s.chars >= UNMAPPED_FONT_SHARE:
        reasons.append("font_without_unicode_map")
    full_page_image = s.image_coverage >= FULL_PAGE_IMAGE
    if full_page_image and s.text_coverage / s.image_coverage < MIN_TEXT_TO_IMAGE:
        reasons.append("image_dominant")
    if reasons:
        return Verdict("unreadable", reasons)

    flags = []
    if s.undecodable_chars:
        flags.append("unmapped_glyphs")
    if full_page_image:
        flags.append("full_page_image_under_text")
    if s.unmapped_font_chars:
        flags.append("some_glyphs_in_unmapped_fonts")
    return Verdict("readable", flags=flags)


def is_decodable(text: str) -> bool:
    if text.startswith("(cid:"):
        return False
    for ch in text:
        if ch == "\ufffd" or 0xE000 <= ord(ch) <= 0xF8FF:
            return False
        if unicodedata.category(ch) == "Cc" and ch not in "\t\n\r":
            return False
    return True


def _name(value: Any) -> str | None:
    value = resolve1(value)
    if isinstance(value, PSLiteral):
        name = value.name
        return name.decode("latin-1") if isinstance(name, bytes) else str(name)
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return None if value is None else str(value)


def _glyph_names_resolve(encoding: Any) -> bool:
    """Whether most glyph names in an encoding's /Differences map to unicode."""
    if not isinstance(encoding, dict):
        return True  # a named base encoding
    names = [
        _name(n) for n in resolve1(encoding.get("Differences")) or [] if isinstance(n, PSLiteral)
    ]
    if not names:
        return True
    resolved = 0
    for n in names:
        try:
            name2unicode(n)
            resolved += 1
        except (KeyError, ValueError):
            pass
    return resolved / len(names) >= MIN_NAMED_GLYPHS


def font_info(font: dict) -> dict[str, Any]:
    """Describe a font dictionary and decide whether it carries a unicode mapping."""
    name = _name(font.get("BaseFont")) or "unknown"
    subtype = _name(font.get("Subtype"))
    encoding_obj = resolve1(font.get("Encoding"))
    encoding = (
        "embedded CMap"
        if isinstance(encoding_obj, PDFStream)
        else "Differences"
        if isinstance(encoding_obj, dict)
        else _name(encoding_obj)
    )
    to_unicode = font.get("ToUnicode") is not None
    mapped = True
    if not to_unicode:
        if subtype == "Type0":
            descendant = resolve1((resolve1(font.get("DescendantFonts")) or [None])[0]) or {}
            ordering = resolve1((resolve1(descendant.get("CIDSystemInfo")) or {}).get("Ordering"))
            ordering = ordering.decode("latin-1") if isinstance(ordering, bytes) else ordering
            # Identity CIDs are glyph ids; only a predefined collection implies characters.
            mapped = ordering not in (None, "Identity")
        elif subtype == "Type3":
            mapped = encoding_obj is not None and _glyph_names_resolve(encoding_obj)
        elif encoding_obj is None and name.split("+")[-1] not in STANDARD_14:
            flags = resolve1((resolve1(font.get("FontDescriptor")) or {}).get("Flags")) or 0
            mapped = not (int(flags) & SYMBOLIC_FLAG)
    return {
        "name": name,
        "subtype": subtype,
        "encoding": encoding,
        "to_unicode": to_unicode,
        "unicode_mapped": mapped,
    }


def _page_fonts(resources: Any, seen: set[int] | None = None) -> list[dict]:
    """Font dictionaries on a page, including those inside Form XObjects."""
    seen = set() if seen is None else seen
    resources = resolve1(resources) or {}
    if id(resources) in seen:
        return []
    seen.add(id(resources))
    fonts = [resolve1(f) for f in (resolve1(resources.get("Font")) or {}).values()]
    for xobj in (resolve1(resources.get("XObject")) or {}).values():
        xobj = resolve1(xobj)
        if isinstance(xobj, PDFStream) and _name(xobj.attrs.get("Subtype")) == "Form":
            fonts += _page_fonts(xobj.attrs.get("Resources"), seen)
    return [f for f in fonts if isinstance(f, dict)]


def _clipped_area(objs: list[dict], bbox: tuple[float, float, float, float]) -> float:
    x0, top, x1, bottom = bbox
    total = 0.0
    for o in objs:
        w = min(o["x1"], x1) - max(o["x0"], x0)
        h = min(o["bottom"], bottom) - max(o["top"], top)
        if w > 0 and h > 0:
            total += w * h
    return total


def broken_fonts(chars: list[dict]) -> set[str]:
    """Fonts most of whose distinct glyphs on the page are undecodable."""
    distinct: dict[str, set[str]] = {}
    bad: dict[str, set[str]] = {}
    for c in chars:
        distinct.setdefault(c["fontname"], set()).add(c["text"])
        if not is_decodable(c["text"]):
            bad.setdefault(c["fontname"], set()).add(c["text"])
    return {f for f, glyphs in bad.items() if len(glyphs) / len(distinct[f]) > BROKEN_FONT_DISTINCT}


def page_signals(page: pdfplumber.page.Page, fonts: dict[str, dict]) -> PageSignals:
    area = page.width * page.height
    chars = page.chars
    unmapped = {
        c["fontname"]
        for c in chars
        if c["fontname"] in fonts and not fonts[c["fontname"]]["unicode_mapped"]
    }
    broken = broken_fonts(chars)
    return PageSignals(
        chars=len(chars),
        undecodable_chars=sum(not is_decodable(c["text"]) for c in chars),
        broken_font_chars=sum(c["fontname"] in broken for c in chars),
        unmapped_font_chars=sum(c["fontname"] in unmapped for c in chars),
        # Overlaps are summed, then capped: scans are one image or abutting strips.
        image_coverage=min(1.0, _clipped_area(page.images, page.bbox) / area) if area else 0.0,
        text_coverage=min(1.0, _clipped_area(chars, page.bbox) / area) if area else 0.0,
        vector_objects=len(page.lines) + len(page.rects) + len(page.curves),
        unmapped_fonts=tuple(sorted(unmapped)),
    )


def assess_page(page: pdfplumber.page.Page) -> tuple[PageSignals, Verdict, dict[str, dict]]:
    """The gate for one page: its signals, its verdict, and the fonts it uses."""
    fonts: dict[str, dict] = {}
    for f in _page_fonts(page.page_obj.resources):
        info = font_info(f)
        prior = fonts.get(info["name"])
        # Same BaseFont twice: unmapped wins, so the gate errs toward unreadable.
        if prior is None or (prior["unicode_mapped"] and not info["unicode_mapped"]):
            fonts[info["name"]] = info
    signals = page_signals(page, fonts)
    return signals, judge(signals), fonts


def check_pdf_page_count(path: Path, max_pages: int) -> None:
    """Reject runaway PDFs in O(1) via PDFium before pdfminer walks large xref tables."""
    try:
        with PDFIUM_LOCK:
            doc = pypdfium2.PdfDocument(path)
            try:
                count = len(doc)
            finally:
                doc.close()
    except Exception:
        # Malformed PDFs fall through so pdfplumber raises its standard diagnostic error.
        return
    if count > max_pages:
        raise LimitExceeded(
            "too_many_pages",
            f"PDF has {count} pages, exceeding max_pdf_pages ({max_pages})",
            actual=count,
            limit=max_pages,
        )


@contextmanager
def open_pdf(path: Path) -> Iterator[pdfplumber.PDF]:
    """Open a pdfplumber PDF without triggering `pdf.close()`'s eager `self.pages` walk.

    `pdfplumber.PDF.close()` calls `self.flush_cache()` (which deletes `_pages`) and then
    iterates `self.pages`, re-materializing every page in the document on exit.
    """
    pdf = pdfplumber.open(path)
    try:
        yield pdf
    finally:
        pdf.flush_cache()
        if not pdf.stream_is_external:
            pdf.stream.close()


def iter_pdf_pages(
    pdf: pdfplumber.PDF, *, max_pages: int | None = None
) -> Iterator[tuple[int, Page]]:
    """Yield `(1-based page number, Page)` one page at a time, closing each page after use."""
    doctop = 0
    gen = PDFPage.create_pages(pdf.doc)
    number = 0
    while True:
        try:
            raw_page = next(gen)
        except StopIteration:
            break
        except Exception as exc:
            raise PdfminerException(exc) from exc
        number += 1
        if max_pages is not None and number > max_pages:
            raise LimitExceeded(
                "too_many_pages",
                f"PDF exceeds max_pdf_pages ({max_pages})",
                actual=number,
                limit=max_pages,
            )
        page = Page(pdf, raw_page, page_number=number, initial_doctop=doctop)
        doctop += page.height
        try:
            yield number, page
        finally:
            page.close()


def probe_pdf(path: Path, *, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    check_file_bytes(path, limits)
    check_pdf_page_count(path, limits.max_pdf_pages)
    pages, all_fonts = [], {}
    counts = {"readable": 0, "unreadable": 0, "blank": 0}
    with open_pdf(path) as pdf:
        for number, page in iter_pdf_pages(pdf, max_pages=limits.max_pdf_pages):
            signals, verdict, fonts = assess_page(page)
            counts[verdict.verdict] += 1
            for name, info in fonts.items():
                if name not in all_fonts or not info["unicode_mapped"]:
                    all_fonts[name] = info
            pages.append(
                {
                    "page": number,
                    **asdict(verdict),
                    **{
                        k: round(v, 4) if isinstance(v, float) else v
                        for k, v in asdict(signals).items()
                    },
                }
            )
    return {
        "pages": len(pages),
        **counts,
        "fonts": sorted(all_fonts.values(), key=lambda f: f["name"]),
        "page_details": pages,
    }
