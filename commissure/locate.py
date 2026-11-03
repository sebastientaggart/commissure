"""Map a character offset in extracted text to the block that contains it.

Offsets follow the parser's half-open spans: a block matches when
``start <= offset < end``. An offset outside every block is a miss (``None``),
never a clamped silent hit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BlockLocation:
    """Where an offset sits in ``extracted_text.structure``."""

    page: int | None
    sheet: str | None
    heading_path: tuple[str, ...] | None
    via_ocr: bool
    confidence: float | None
    kind: str


def locate_offset(
    text: str,
    structure: Mapping[str, Any] | None,
    offset: int,
) -> BlockLocation | None:
    """Return the containing block for ``offset``, or ``None`` when none matches.

    Among matching blocks, the narrowest span wins; equal widths prefer the later
    block in ``structure["blocks"]``. Zero-width blocks (``start == end``) contain
    nothing. Missing ``start`` or ``end`` excludes a block from candidates.
    """
    if offset < 0 or offset >= len(text):
        return None
    blocks = (structure or {}).get("blocks") or []
    best: tuple[int, int, Mapping[str, Any]] | None = None  # (width, -index, block)
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            continue
        start, end = block.get("start"), block.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if start == end:
            continue
        if not (start <= offset < end):
            continue
        width = end - start
        candidate = (width, -index, block)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return None
    block = best[2]
    kind = str(block.get("kind") or "")
    heading = block.get("heading_path")
    return BlockLocation(
        page=block.get("page") if isinstance(block.get("page"), int) else None,
        sheet=block.get("name") if kind == "sheet" and isinstance(block.get("name"), str) else None,
        heading_path=tuple(heading) if isinstance(heading, list) else None,
        via_ocr="ocr" in (block.get("flags") or []),
        confidence=block.get("confidence")
        if isinstance(block.get("confidence"), (int, float))
        else None,
        kind=kind,
    )
