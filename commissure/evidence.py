"""Evidence: verified pointers from a record into the item versions it rests on.

A recipe builds `Evidence` from an `Item` its stage received and embeds it anywhere in
its payload. When the record is stored, core walks the payload, checks every pointer
against the subject, resolves where it lands (locator, page, OCR origin, OCR
confidence), writes those values into the stored payload, and indexes one `evidence`
row per pointer with its JSON Pointer (`AGENTS.md`, "Human-verifiable results").

Three anchors, narrowest first:

    Evidence.span(item, start, end, kind=...)  an exact slice of the text the stage got
    Evidence.on_page(item, page, kind=...)     one page of that text
    Evidence.whole_item(item, kind=...)        the item as a whole, for text the recipe
                                               cleaned or decoded and cannot point into

`via_ocr is None` marks evidence core has not resolved yet. Resolved evidence (read back
from an upstream record) is accepted only when it is identical to evidence in a record
the outcome consumed; anything else carrying resolved values was not built by core.
A failed check raises `EvidenceError`, which fails the run: a pointer that does not
match is a recipe bug, not bad data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from commissure.locate import locate_offset

if TYPE_CHECKING:
    from commissure.recipes import Group, Item


class EvidenceError(Exception):
    """Evidence in a payload does not check out against the stage's subject."""


class Evidence(BaseModel):
    """A pointer into one item version; core checks it and fills the resolved fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)  # recipe-defined: what this evidence supports
    item_version_id: int
    extracted_text_id: int | None = None  # the text row offsets index into
    start: int | None = None  # half-open span [start, end) of that text
    end: int | None = None
    text: str | None = None  # the exact slice, for spans
    page: int | None = None  # the pointer for page evidence; resolved by core for spans
    # Resolved by core when the record is stored; a recipe never sets these.
    locator: str | None = None
    via_ocr: bool | None = None
    confidence: float | None = None  # lowest OCR confidence of the pages it touches

    @classmethod
    def span(cls, item: Item, start: int, end: int, *, kind: str) -> Evidence:
        """`item.text[start:end]`, exactly. Raises `ValueError` outside the text."""
        text = _text_of(item)
        if not 0 <= start < end <= len(text):
            raise ValueError(f"span [{start}, {end}) outside {item.locator} ({len(text)} chars)")
        return cls(
            kind=kind,
            item_version_id=item.item_version_id,
            extracted_text_id=item.extracted_text_id,
            start=start,
            end=end,
            text=text[start:end],
        )

    @classmethod
    def on_page(cls, item: Item, page: int, *, kind: str) -> Evidence:
        """One page of the item's text; the page must exist in its structure."""
        _text_of(item)
        if page not in _pages(item.structure):
            raise ValueError(f"{item.locator} has no page {page}")
        return cls(
            kind=kind,
            item_version_id=item.item_version_id,
            extracted_text_id=item.extracted_text_id,
            page=page,
        )

    @classmethod
    def whole_item(cls, item: Item, *, kind: str) -> Evidence:
        """The item as a whole: the text could not be pointed into exactly."""
        return cls(
            kind=kind,
            item_version_id=item.item_version_id,
            extracted_text_id=item.extracted_text_id,
        )


def _text_of(item: Item) -> str:
    if item.text is None or item.extracted_text_id is None:
        raise ValueError(f"{item.locator} reached the stage without text; declare text= on it")
    return item.text


def _blocks(structure: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return [b for b in (structure or {}).get("blocks") or [] if isinstance(b, Mapping)]


def _pages(structure: Mapping[str, Any] | None) -> set[int]:
    return {
        b["page"]
        for b in _blocks(structure)
        if b.get("kind") == "page" and isinstance(b.get("page"), int)
    }


def _ocr(blocks: list[Mapping[str, Any]]) -> tuple[bool, float | None]:
    """Whether any block was recognised by OCR, and the lowest confidence among them."""
    ocr = [b for b in blocks if "ocr" in (b.get("flags") or [])]
    scores = [b["confidence"] for b in ocr if isinstance(b.get("confidence"), int | float)]
    return bool(ocr), (float(min(scores)) if scores else None)


def find(value: Any, pointer: str = "") -> Iterator[tuple[str, Evidence]]:
    """Every `Evidence` inside `value`, with its RFC 6901 JSON Pointer."""
    if isinstance(value, Evidence):
        yield pointer, value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from find(getattr(value, name), f"{pointer}/{_escape(name)}")
    elif isinstance(value, list | tuple):
        for index, entry in enumerate(value):
            yield from find(entry, f"{pointer}/{index}")
    elif isinstance(value, Mapping):
        for key, entry in value.items():
            yield from find(entry, f"{pointer}/{_escape(str(key))}")


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _set(document: Any, pointer: str, value: Any) -> None:
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]
    target = document
    try:
        for token in tokens[:-1]:
            target = target[int(token)] if isinstance(target, list) else target[token]
        if isinstance(target, list):
            target[int(tokens[-1])] = value
        else:
            target[tokens[-1]] = value
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        # A custom serializer moved or dropped the field: the stored payload would lose
        # the resolved pointer, so refuse rather than store evidence nobody can find.
        raise EvidenceError(f"evidence at {pointer} is not in the serialized payload") from exc


def _members(subject_input: Item | Group) -> tuple[Item, ...]:
    # A Group (group and run subjects) has `items`; an Item is its own only member.
    return tuple(subject_input.items) if hasattr(subject_input, "items") else (subject_input,)


def consumed_evidence(subject_input: Item | Group) -> set[Evidence]:
    """Evidence in every upstream record the subject consumed (already checked by core)."""
    payloads = [p for item in _members(subject_input) for p in item.upstream.values()]
    if hasattr(subject_input, "items"):  # a Group's own upstream: {need: {group key: payload}}
        payloads += [p for by_key in subject_input.upstream.values() for p in by_key.values()]
    return {e for p in payloads for _, e in find(p)}


def resolve(payload: BaseModel, subject_input: Item | Group) -> tuple[dict, list[dict]]:
    """Check every `Evidence` in `payload`; return the serialized payload and index rows.

    The returned payload has each new pointer's resolved fields filled in, so the stored
    record (and its hash) carries what downstream stages and deliverables show.
    """
    document = payload.model_dump(mode="json")
    found = list(find(payload))
    if not found:
        return document, []
    members = {i.item_version_id: i for i in _members(subject_input)}
    upstream = consumed_evidence(subject_input)
    rows = []
    for pointer, ev in found:
        if ev.via_ocr is not None:
            if ev not in upstream:
                raise EvidenceError(
                    f"evidence at {pointer} carries resolved fields (locator, via_ocr,"
                    " confidence) but matches no evidence in a consumed upstream record;"
                    " build it with Evidence.span, Evidence.on_page, or Evidence.whole_item"
                )
            resolved, copied = ev, True
        else:
            resolved, copied = _check(pointer, ev, members), False
            _set(document, pointer, resolved.model_dump(mode="json"))
        rows.append({"pointer": pointer, "copied": copied, **resolved.model_dump()})
    return document, rows


def _check(pointer: str, ev: Evidence, members: Mapping[int, Item]) -> Evidence:
    if ev.locator is not None or ev.confidence is not None:
        raise EvidenceError(f"evidence at {pointer} sets locator or confidence; core resolves them")
    item = members.get(ev.item_version_id)
    if item is None:
        raise EvidenceError(
            f"evidence at {pointer} points at item version {ev.item_version_id},"
            " which is not part of this subject"
        )
    has_span = ev.start is not None or ev.end is not None or ev.text is not None
    if ev.extracted_text_id is not None and ev.extracted_text_id != item.extracted_text_id:
        raise EvidenceError(
            f"evidence at {pointer} names text row {ev.extracted_text_id}; the stage was given"
            f" {item.extracted_text_id} for {item.locator}"
        )
    blocks = _blocks(item.structure)
    if has_span:
        if ev.page is not None:
            raise EvidenceError(f"evidence at {pointer} sets a page on a span; core resolves it")
        if item.text is None or ev.extracted_text_id is None:
            raise EvidenceError(
                f"evidence at {pointer} is a span into text the stage was not given"
            )
        if ev.start is None or ev.end is None or not 0 <= ev.start < ev.end <= len(item.text):
            raise EvidenceError(
                f"evidence at {pointer} span [{ev.start}, {ev.end}) is outside {item.locator}"
                f" ({len(item.text)} chars)"
            )
        if item.text[ev.start : ev.end] != ev.text:
            raise EvidenceError(
                f"evidence at {pointer} claims {ev.text!r} but {item.locator}"
                f" [{ev.start}, {ev.end}) is {item.text[ev.start : ev.end]!r}"
            )
        start_at = locate_offset(item.text, item.structure, ev.start)
        touched = [
            b
            for b in blocks
            if isinstance(b.get("start"), int)
            and isinstance(b.get("end"), int)
            and b["start"] < ev.end
            and ev.start < b["end"]
        ]
        via_ocr, confidence = _ocr(touched)
        page = start_at.page if start_at is not None else None
    elif ev.page is not None:
        if ev.page not in _pages(item.structure):
            raise EvidenceError(
                f"evidence at {pointer} names page {ev.page}, not in {item.locator}"
            )
        via_ocr, confidence = _ocr(
            [b for b in blocks if b.get("kind") == "page" and b.get("page") == ev.page]
        )
        page = ev.page
    else:
        via_ocr, confidence = _ocr(blocks)
        confidence, page = None, None  # the item as a whole has no single page or score
    return ev.model_copy(
        update={"locator": item.locator, "page": page, "via_ocr": via_ocr, "confidence": confidence}
    )


def index(conn: sqlite3.Connection, record_id: int, rows: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO evidence (record_id, pointer, kind, copied, item_version_id,"
        " extracted_text_id, span_start, span_end, page, via_ocr, confidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record_id,
                r["pointer"],
                r["kind"],
                int(r["copied"]),
                r["item_version_id"],
                r["extracted_text_id"],
                r["start"],
                r["end"],
                r["page"],
                int(r["via_ocr"]),
                r["confidence"],
            )
            for r in rows
        ],
    )
