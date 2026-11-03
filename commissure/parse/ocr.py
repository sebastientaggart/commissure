"""OCR parser for PDFs: the text layer where the gate accepts a page, Tesseract where not.

`ocr` is a parser like any other (Identity 5): it writes its own `extracted_text` row
under its own computed version, which includes the Tesseract binary version and the
language it reads. It is never the default for `.pdf`, because recognition is slow and
needs a local binary. Run it explicitly (`run --step parse --parser ocr`) and let a
recipe stage prefer its rows with `text=("ocr", "default")`.

Per page, the probe gate decides. A readable page takes pdfplumber's text and tables,
exactly as the `pdfplumber` parser would. An unreadable page (image-only, broken fonts)
is rendered with PDFium at `OCR_DPI` and read by Tesseract; it becomes readable with the
flag `ocr` and its mean word confidence when the result has at least `OCR_MIN_CHARS`
characters and a mean confidence of at least `OCR_MIN_CONFIDENCE`, otherwise it stays
unreadable with `ocr_no_text` or `ocr_low_confidence` added to its reasons. A page that
fails is asked once for its orientation (Tesseract OSD); when OSD proposes a rotation, the
image is turned and read again, the better result is kept, and a recognised page that was
turned carries the flag `ocr_rotated_<degrees>`. PDFium already honours `/Rotate`; this is
for content drawn sideways or upside down inside the page. Blank pages
are not recognised. Recognised pages are listed in the `ocr_pages` issue, so an item with
any recognised page gets the outcome `warning`: recognised text is lower trust than a
text layer, and the consumer should know which pages it came from.

Tesseract runs as a local child process. Nothing leaves the machine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pypdfium2

from commissure.limits import DEFAULT_LIMITS, Limits, check_file_bytes
from commissure.parse.model import ParsedDoc
from commissure.parse.pdf import _PDFIUM, _parse, _plumber_page_text
from commissure.probe.pdf import check_pdf_page_count

OCR_LANG = "eng"
OCR_DPI = 300  # measured on real scans: higher confidence overall than 400 DPI, and faster
OCR_MIN_CHARS = 20
OCR_MIN_CONFIDENCE = 40.0
OCR_TIMEOUT_S = 120
_SPACES = re.compile(r"[ \t]+")


class ParserUnavailable(Exception):
    """The parser needs a local tool that is not installed. A configuration error."""


def tesseract_version() -> str:
    """Tesseract's version string, or `ParserUnavailable` when the binary is missing."""
    try:
        import pytesseract

        version = pytesseract.get_tesseract_version()
    except Exception as exc:  # TesseractNotFoundError, or a broken install
        raise ParserUnavailable(
            "the ocr parser needs the tesseract binary (for example `brew install tesseract`"
            f" or `apt install tesseract-ocr`): {type(exc).__name__}: {exc}"
        ) from None
    return f"tesseract={version};lang={OCR_LANG}"


def ocr_image(image) -> tuple[str, float | None]:
    """Recognise one page image: (text in reading order, mean word confidence or None)."""
    import pytesseract

    data = pytesseract.image_to_data(
        image, lang=OCR_LANG, output_type=pytesseract.Output.DICT, timeout=OCR_TIMEOUT_S
    )
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    for word, conf, block, par, line in zip(
        data["text"],
        data["conf"],
        data["block_num"],
        data["par_num"],
        data["line_num"],
        strict=True,
    ):
        word = _SPACES.sub(" ", str(word)).strip()
        if not word:
            continue
        lines.setdefault((block, par, line), []).append(word)
        if float(conf) >= 0:  # -1 marks layout elements, not words
            confidences.append(float(conf))
    text = "\n".join(" ".join(words) for _, words in sorted(lines.items())).strip()
    mean = sum(confidences) / len(confidences) if confidences else None
    return text, mean


def orientation(image) -> int:
    """Degrees clockwise Tesseract says the image must turn to read upright; 0 if unknown."""
    import pytesseract

    try:
        osd = pytesseract.image_to_osd(
            image, output_type=pytesseract.Output.DICT, timeout=OCR_TIMEOUT_S
        )
    except pytesseract.TesseractError:  # too few characters to judge, or no osd data
        return 0
    return int(osd.get("rotate", 0)) % 360


def _judge(text: str, confidence: float | None) -> str | None:
    """The reason a recognised page is still unreadable, or None when it is readable."""
    if len(text) < OCR_MIN_CHARS:
        return "ocr_no_text"
    if confidence is None or confidence < OCR_MIN_CONFIDENCE:
        return "ocr_low_confidence"
    return None


def parse_ocr(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
    tesseract_version()  # fail before any work when the binary is missing
    check_file_bytes(path, limits)
    check_pdf_page_count(path, limits.max_pdf_pages)
    with _PDFIUM:
        doc = pypdfium2.PdfDocument(path)
    try:

        def ocr_page(number: int) -> tuple[str, float | None, str | None, list[str]]:
            with _PDFIUM:
                page = doc[number - 1]
                bitmap = page.render(scale=OCR_DPI / 72, grayscale=True)
                image = bitmap.to_pil()
                page.close()
            try:
                text, confidence = ocr_image(image)
                reason, flags = _judge(text, confidence), []
                degrees = orientation(image) if reason else 0
                if degrees:
                    # PIL turns counter-clockwise; OSD's `rotate` is clockwise.
                    turned, turned_conf = ocr_image(image.rotate(-degrees, expand=True))
                    if (turned_conf or 0) > (confidence or 0):
                        text, confidence = turned, turned_conf
                        reason, flags = _judge(text, confidence), [f"ocr_rotated_{degrees}"]
            except RuntimeError as exc:  # pytesseract raises RuntimeError on timeout
                if "timeout" not in str(exc).lower():
                    raise
                return "", None, "ocr_timeout", []
            return text, confidence, reason, flags

        return _parse(path, _plumber_page_text, ocr_page=ocr_page, limits=limits)
    finally:
        with _PDFIUM:
            doc.close()
