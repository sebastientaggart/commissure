"""Text extraction primitives for line flattening, offset mapping, and sentence bounds.

Recipes often scan multi-line text for identifiers, citations, and currency statements that
wrap across line breaks (both in born-digital text layers and in OCR), then build a display
sentence alongside a verified ``Evidence.span`` into the original ``Item``.

``lines_of`` splits ``item.text`` into stripped non-blank ``Line`` objects annotated via
``locate_offset``. ``Flat.of`` joins those lines into one searchable string while recording
where each line starts; a line ending in a hyphen (``45 CFR 1355-`` / ``20``) joins without
a space so wrapped tokens match as one. ``Flat.item_span`` maps any match in the flattened
string back to exact character offsets in ``item.text``, and ``Flat.span_evidence`` returns
both the surrounding display sentence and the verified ``Evidence.span`` in one call.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from commissure.evidence import Evidence
from commissure.locate import locate_offset

if TYPE_CHECKING:
    from commissure.recipes import Item

# Glyph-broken PDF text layers decode to ASCII punctuation (~0.26 to 0.27 of non-space
# characters), whereas readable prose, forms, and contact lists stay below ~0.041.
# Function-word rates fail on real contact lists and tables; symbol share separates them.
SYMBOL_RATE_LIMIT: float = 0.12
SYMBOLS: frozenset[str] = frozenset("!\"#$%&'()*+/<=>@[\\]^_{|}~")

ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "u.s",
        "p.l",
        "pub",
        "no",
        "dep't",
        "dept",
        "sec",
        "secs",
        "v",
        "vs",
        "inc",
        "al",
        "et",
        "fed",
        "reg",
        "doc",
        "vol",
        "pt",
        "pts",
        "ch",
        "subch",
        "par",
        "cf",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "e.g",
        "i.e",
        "st",
        "stat",
        "seq",
        "servs",
        "serv",
        "servc",
        "l",
        "c.f.r",
        "u.s.c",
        "fr",
        "fig",
        "mr",
        "mrs",
        "ms",
        "dr",
        "jr",
        "sr",
        "co",
        "corp",
        "ltd",
        "ass'n",
        "assn",
        "div",
        "admin",
        "gov",
        "rev",
    }
)


def symbol_rate(text: str) -> float:
    """Return the share of non-whitespace characters in ``text`` that belong to ``SYMBOLS``.

    Returns ``0.0`` when ``text`` contains no non-whitespace characters.
    """
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return 0.0
    return sum(1 for ch in chars if ch in SYMBOLS) / len(chars)


@dataclass(frozen=True)
class Line:
    """One stripped non-blank line from extracted text, with its block metadata."""

    text: str
    offset: int
    page: int | None = None
    via_ocr: bool = False
    confidence: float | None = None


def lines_of(text: str, structure: Mapping[str, Any] | None = None) -> list[Line]:
    """Non-blank stripped lines with their page and OCR fields (``locate_offset``, no clamp)."""
    out: list[Line] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        without_nl = raw.rstrip("\r\n")
        stripped = without_nl.strip()
        if stripped:
            lead = len(without_nl) - len(without_nl.lstrip())
            line_offset = offset + lead
            loc = locate_offset(text, structure, line_offset)
            out.append(
                Line(
                    text=stripped,
                    offset=line_offset,
                    page=loc.page if loc else None,
                    via_ocr=loc.via_ocr if loc else False,
                    confidence=loc.confidence if loc else None,
                )
            )
        offset += len(raw)
    return out


def trim_line(line: Line, start: int) -> Line | None:
    """Drop ``line.text[:start]`` and leading spaces; keep ``offset`` on the remaining text."""
    rest = line.text[start:]
    stripped = rest.strip()
    if not stripped:
        return None
    pad = len(rest) - len(rest.lstrip())
    return Line(
        text=stripped,
        offset=line.offset + start + pad,
        page=line.page,
        via_ocr=line.via_ocr,
        confidence=line.confidence,
    )


def page_lines(lines: Sequence[Line], pages: int) -> list[Line]:
    """Lines on the first ``pages`` pages (all lines when the text has no page blocks)."""
    if all(line.page is None for line in lines):
        return list(lines)
    first = min(line.page for line in lines if line.page is not None)
    return [line for line in lines if line.page is not None and line.page < first + pages]


def sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Return the ``(start, end)`` bounds of the sentence around ``[start, end)``.

    Splits on ``.``, ``?``, or ``!`` followed by whitespace and an uppercase letter or
    opening quote/parenthesis, ignoring periods that end a known abbreviation, a dotted
    token (``U.S.``, ``E.D.``), or a single-letter initial.
    """
    if not 0 <= start <= end <= len(text):
        raise ValueError(f"span [{start}, {end}) outside text ({len(text)} chars)")

    def is_boundary(i: int) -> bool:
        if text[i] not in ".?!" or i + 2 > len(text):
            return False
        if not text[i + 1 : i + 2].isspace():
            return False
        j = i + 2
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and not (text[j].isupper() or text[j] in '("“'):
            return False
        word = re.search(r"([A-Za-z.']+)$", text[max(0, i - 12) : i])
        token = (word.group(1) if word else "").lower().strip(".")
        # A dotted token ("E.D.", "U.S.") or an initial is an abbreviation, not an ending.
        return not (token in ABBREVIATIONS or len(token) == 1 or "." in token)

    s = start
    while s > 0 and not is_boundary(s - 1):
        s -= 1
    e = end
    while e < len(text) and not is_boundary(e):
        e += 1
    return s, min(len(text), e + 1)


@dataclass
class Flat:
    """Lines joined into one string; ``at(pos)`` returns the line a character came from."""

    text: str = ""
    starts: list[int] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    item: Item | None = None

    @classmethod
    def of(cls, lines: Sequence[Line], item: Item | None = None) -> Flat:
        flat = cls(item=item)
        parts: list[str] = []
        pos = 0
        for i, line in enumerate(lines):
            if i and not (parts[-1].endswith("-") and not parts[-1].endswith(" -")):
                parts.append(" ")
                pos += 1
            flat.starts.append(pos)
            flat.lines.append(line)
            parts.append(line.text)
            pos += len(line.text)
        flat.text = "".join(parts)
        return flat

    def at(self, pos: int) -> Line | None:
        """Return the ``Line`` containing ``pos`` in ``self.text``, or ``None`` if out of range."""
        if not 0 <= pos < len(self.text):
            return None
        i = bisect.bisect_right(self.starts, pos) - 1
        return self.lines[i] if 0 <= i < len(self.lines) else None

    def item_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a half-open span ``[start, end)`` of flattened text onto the item's text.

        A join space between lines covers the newline (and any stripped margins) that
        flattening removed, so the item span includes that gap.
        """
        if not 0 <= start < end <= len(self.text):
            raise ValueError(f"flat span [{start}, {end}) outside flattened text")
        i = bisect.bisect_right(self.starts, start) - 1
        j = bisect.bisect_right(self.starts, end - 1) - 1
        item_start = self.lines[i].offset + (start - self.starts[i])
        item_end = self.lines[j].offset + (end - self.starts[j])
        return item_start, item_end

    def span_evidence(self, start: int, end: int, kind: str = "quote") -> tuple[str, Evidence]:
        """Return ``(sentence_text, Evidence.span(...))`` for ``[start, end)`` in ``self.text``."""
        if self.item is None:
            raise ValueError("Flat.span_evidence needs the Item the lines came from")
        s, e = sentence_bounds(self.text, start, end)
        item_start, item_end = self.item_span(start, end)
        return (
            self.text[s:e].strip(),
            Evidence.span(self.item, item_start, item_end, kind=kind),
        )
