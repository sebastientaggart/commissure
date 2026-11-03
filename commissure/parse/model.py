"""What every parser produces: `ParsedDoc`.

`text` is the searchable text of the item: the blocks' text joined by blank lines, in
reading order. Each block records its `[start, end)` span in `text`, so a search hit
maps back to a page, heading, sheet, or table. Blocks that carry no text (a page the
readability gate rejected, a blank page) are still listed, with an empty span, so the
structure says what was left out and why.

`issues` are machine-readable problems the parser saw. They become the item's outcome
diagnostics; they never alter `text` silently.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

SEPARATOR = "\n\n"


@dataclass
class Block:
    kind: str  # page | heading | paragraph | table | sheet | document
    start: int
    end: int
    page: int | None = None
    level: int | None = None
    heading_path: list[str] | None = None
    name: str | None = None
    table: int | None = None  # index into ParsedDoc.tables
    status: str = "readable"  # readable | unreadable | blank
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    confidence: float | None = None  # mean word confidence (0-100) of a recognised page


@dataclass
class ParsedDoc:
    text: str
    blocks: list[Block]
    tables: list[dict[str, Any]]
    metadata: dict[str, Any]
    issues: list[dict[str, Any]]

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def structure(self) -> dict[str, Any]:
        """Everything except text, as stored in extracted_text.structure."""
        return {
            "blocks": [
                {k: v for k, v in asdict(b).items() if v not in (None, [])} for b in self.blocks
            ],
            "tables": self.tables,
            "metadata": self.metadata,
        }


class DocBuilder:
    """Accumulates blocks and keeps their spans in `text` exact."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0
        self.blocks: list[Block] = []
        self.tables: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {}
        self.issues: list[dict[str, Any]] = []

    def block(self, kind: str, text: str, **attrs: Any) -> Block:
        """Append a block. Whitespace-only text counts as no text (an empty table)."""
        if text.strip():
            if self._parts:
                self._parts.append(SEPARATOR)
                self._length += len(SEPARATOR)
            start = self._length
            self._parts.append(text)
            self._length += len(text)
        else:
            start = self._length
        b = Block(kind=kind, start=start, end=self._length, **attrs)
        self.blocks.append(b)
        return b

    def table(self, rows: list[list[Any]], **where: Any) -> int:
        self.tables.append({**where, "rows": rows})
        return len(self.tables) - 1

    def issue(self, code: str, **detail: Any) -> None:
        self.issues.append({"code": code, **detail})

    def build(self) -> ParsedDoc:
        return ParsedDoc("".join(self._parts), self.blocks, self.tables, self.metadata, self.issues)


def rows_to_text(rows: list[list[Any]]) -> str:
    """Tab-separated lines; None becomes an empty cell."""
    return "\n".join("\t".join("" if v is None else str(v) for v in row) for row in rows)
