"""Structure- and offset-preserving text chunking, adaptive splitting, and quote alignment.

Every `TextChunk` produced by `chunk_text` or `chunk_item` is an exact, unmodified
slice `source_text[chunk.start:chunk.end]` of the input text with `estimate_tokens <=
budget_tokens`, so downstream stages can build `Evidence.span(item, chunk.start + s,
chunk.start + e, kind=...)` directly without searching the full document.

`find_verbatim_span` verifies model-extracted quotes against source text using
character-folded exact search first and `rapidfuzz` partial-ratio alignment second,
always returning the exact verbatim slice and `[start, end)` offsets from the original
source text so `Evidence.span` checks out.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz

from commissure.evidence import Evidence
from commissure.llm import estimate_tokens
from commissure.locate import locate_offset

if TYPE_CHECKING:
    from commissure.recipes import Item

_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n+")
_LINE_BREAK_RE = re.compile(r"\n+")
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])(?:\s+|$)")
_CLAUSE_BREAK_RE = re.compile(r"(?<=[;,:—–\)\]])\s+")
_WORD_BREAK_RE = re.compile(r"\s+")

_FOLD_MAP: dict[str, str] = {
    '"': "'",
    "\u2018": "'",
    "\u2019": "'",
    "\u201b": "'",
    "\u02bc": "'",
    "`": "'",
    "\u201c": "'",
    "\u201d": "'",
    "\u201f": "'",
    "\u00ab": "'",
    "\u00bb": "'",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
}


@dataclass(frozen=True)
class TextChunk:
    """An exact `[start, end)` slice of a source text with structural provenance."""

    text: str
    start: int
    end: int
    index: int = 1
    total: int = 1
    pages: tuple[int, ...] = ()
    sheets: tuple[str, ...] = ()
    heading_path: tuple[str, ...] | None = None
    locator: str | None = None
    item: Item | None = field(default=None, repr=False, compare=False)
    _structure: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    def context_label(self) -> str:
        """Compact positional and structural breadcrumb for model prompts."""
        base = f"{self.index} of {self.total}"
        meta: list[str] = []
        if self.locator:
            meta.append(f"file={self.locator}")
        if self.pages:
            if len(self.pages) == 1:
                meta.append(f"page={self.pages[0]}")
            else:
                meta.append(f"pages={self.pages[0]}-{self.pages[-1]}")
        if self.sheets:
            meta.append(f"sheet={','.join(self.sheets)}")
        if self.heading_path:
            meta.append(f"section={' > '.join(self.heading_path)}")
        if not meta:
            return base
        return f"{base} ({', '.join(meta)})"

    def span(
        self,
        rel_start: int,
        rel_end: int,
        *,
        kind: str,
        item: Item | None = None,
    ) -> Evidence:
        """Build `Evidence.span` for `[rel_start, rel_end)` inside `self.text`."""
        target = item if item is not None else self.item
        if target is None:
            raise ValueError("TextChunk.span() requires an Item (pass item= or use chunk_item)")
        if not 0 <= rel_start < rel_end <= len(self.text):
            raise ValueError(
                f"relative span [{rel_start}, {rel_end}) outside chunk ({len(self.text)} chars)"
            )
        return Evidence.span(target, self.start + rel_start, self.start + rel_end, kind=kind)

    def split_half(
        self,
        *,
        overlap_tokens: int = 0,
        structure: Mapping[str, Any] | None = None,
    ) -> tuple[TextChunk, ...]:
        """Split this chunk into two balanced sub-chunks at the cleanest boundary near midpoint."""
        total_tok = self.tokens
        if total_tok <= 1 or len(self.text.strip()) <= 1:
            return (self,)
        eff_struct = structure if structure is not None else self._structure
        mid_char = len(self.text) // 2
        effective_overlap = max(0, min(overlap_tokens, max(0, total_tok // 3)))

        raw_spans: list[tuple[int, int]] = []
        for pat in _SPLIT_PATTERNS:
            parts = _split_by_regex(self.text, 0, len(self.text), pat)
            if len(parts) >= 2:
                best_k = min(
                    range(1, len(parts)),
                    key=lambda k: abs(parts[k - 1][1] - mid_char),
                )
                first_span = (parts[0][0], parts[best_k - 1][1])
                second_idx = best_k
                if effective_overlap > 0:
                    end_first = parts[best_k - 1][1]
                    while second_idx > 1:
                        cand_start = parts[second_idx - 1][0]
                        if estimate_tokens(self.text[cand_start:end_first]) > effective_overlap:
                            break
                        second_idx -= 1
                second_span = (parts[second_idx][0], parts[-1][1])
                raw_spans = [first_span, second_span]
                break

        if len(raw_spans) <= 1:
            # Force a two-way character split when the chunk has no delimiter boundaries.
            s1 = _trim_span(self.text, 0, mid_char)
            s2 = _trim_span(self.text, mid_char, len(self.text))
            raw_spans = [sp for sp in (s1, s2) if sp is not None]
            if len(raw_spans) <= 1:
                return (self,)

        source_text = self.item.text if (self.item and self.item.text is not None) else None
        out: list[TextChunk] = []
        n = len(raw_spans)
        for idx, (rel_s, rel_e) in enumerate(raw_spans, start=1):
            abs_s = self.start + rel_s
            abs_e = self.start + rel_e
            sub_text = self.text[rel_s:rel_e]
            pages, sheets, heading = _resolve_span_structure(
                source_text, eff_struct, abs_s, abs_e, fallback=self
            )
            out.append(
                TextChunk(
                    text=sub_text,
                    start=abs_s,
                    end=abs_e,
                    index=idx,
                    total=n,
                    pages=pages,
                    sheets=sheets,
                    heading_path=heading,
                    locator=self.locator,
                    item=self.item,
                    _structure=eff_struct,
                )
            )
        return tuple(out)


def _trim_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    return (start, end)


def _split_by_regex(
    text: str, start: int, end: int, pattern: re.Pattern[str]
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cur = start
    for m in pattern.finditer(text, start, end):
        trimmed = _trim_span(text, cur, m.start())
        if trimmed is not None:
            spans.append(trimmed)
        cur = m.end()
    tail = _trim_span(text, cur, end)
    if tail is not None:
        spans.append(tail)
    return spans


def _split_by_chars(text: str, start: int, end: int, budget_tokens: int) -> list[tuple[int, int]]:
    """Hard fallback for a single unbroken token exceeding `budget_tokens`."""
    spans: list[tuple[int, int]] = []
    cur = start
    # Each UTF-8 char is at most 4 bytes (1 token in estimate_tokens), so `budget_tokens`
    # ASCII chars is at most `budget_tokens` tokens; step conservatively.
    while cur < end:
        lo = cur + 1
        hi = end
        best = lo
        while lo <= hi:
            mid = (lo + hi) // 2
            if estimate_tokens(text[cur:mid]) <= budget_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        trimmed = _trim_span(text, cur, best)
        if trimmed is not None:
            spans.append(trimmed)
        cur = best
    return spans


_SPLIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    _PARAGRAPH_BREAK_RE,
    _LINE_BREAK_RE,
    _SENTENCE_BREAK_RE,
    _CLAUSE_BREAK_RE,
    _WORD_BREAK_RE,
)


def _split_span_to_budget(
    text: str,
    start: int,
    end: int,
    budget_tokens: int,
    level: int = 0,
) -> list[tuple[int, int]]:
    """Recursively break `text[start:end]` into atomic `(s, e)` spans each `<= budget_tokens`."""
    trimmed = _trim_span(text, start, end)
    if trimmed is None:
        return []
    s, e = trimmed
    if estimate_tokens(text[s:e]) <= budget_tokens:
        return [(s, e)]

    for pat_idx in range(level, len(_SPLIT_PATTERNS)):
        parts = _split_by_regex(text, s, e, _SPLIT_PATTERNS[pat_idx])
        if len(parts) > 1:
            out: list[tuple[int, int]] = []
            for ps, pe in parts:
                if estimate_tokens(text[ps:pe]) <= budget_tokens:
                    out.append((ps, pe))
                else:
                    out.extend(_split_span_to_budget(text, ps, pe, budget_tokens, pat_idx + 1))
            return out

    return _split_by_chars(text, s, e, budget_tokens)


def _structural_seed_spans(
    text: str,
    start: int,
    end: int,
    structure: Mapping[str, Any] | None,
) -> list[tuple[int, int]]:
    """Use heading or sheet block starts as top-level section boundaries when available."""
    if not structure:
        return [(start, end)]
    raw_blocks = structure.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        return [(start, end)]

    cut_points: set[int] = {start, end}
    for b in raw_blocks:
        if not isinstance(b, Mapping):
            continue
        kind = b.get("kind")
        b_start = b.get("start")
        if kind in ("heading", "sheet") and isinstance(b_start, int) and start < b_start < end:
            cut_points.add(b_start)

    if len(cut_points) <= 2:
        return [(start, end)]

    ordered = sorted(cut_points)
    spans: list[tuple[int, int]] = []
    for a, b in zip(ordered, ordered[1:], strict=False):
        trimmed = _trim_span(text, a, b)
        if trimmed is not None:
            spans.append(trimmed)
    return spans or [(start, end)]


def _pack_spans(
    text: str,
    spans: Sequence[tuple[int, int]],
    *,
    budget_tokens: int,
    overlap_tokens: int = 0,
) -> list[tuple[int, int]]:
    """Merge contiguous atomic spans `(s_i, e_i)` into `(s_first, e_last) <= budget_tokens`.

    Uses binary search over cumulative byte offsets to find the furthest span `j` such that
    `estimate_tokens(text[s_i:e_j]) <= budget_tokens` in O(log N) token evaluations per chunk.
    """
    n = len(spans)
    if n == 0:
        return []
    if n == 1:
        return [spans[0]]

    effective_overlap = max(0, min(overlap_tokens, max(0, budget_tokens // 3)))
    ends = [e for _, e in spans]
    packed: list[tuple[int, int]] = []
    i = 0

    while i < n:
        s_i = spans[i][0]
        # Approximate character reach for `budget_tokens` (~4 bytes per token) to narrow bisect.
        approx_end_char = s_i + budget_tokens * 4
        hi_guess = min(n - 1, bisect_right(ends, approx_end_char, lo=i))
        # Expand hi_guess if multi-byte estimate still fits.
        while (
            hi_guess + 1 < n
            and estimate_tokens(text[s_i : spans[hi_guess + 1][1]]) <= budget_tokens
        ):
            hi_guess = min(n - 1, hi_guess + max(1, (n - hi_guess) // 2))

        lo = i
        hi = hi_guess
        best = i
        while lo <= hi:
            mid = (lo + hi) // 2
            if estimate_tokens(text[s_i : spans[mid][1]]) <= budget_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        packed.append((s_i, spans[best][1]))
        if best >= n - 1:
            break

        next_i = best + 1
        if effective_overlap > 0:
            back = next_i
            end_char = spans[best][1]
            while back > i + 1:
                cand_start = spans[back - 1][0]
                if estimate_tokens(text[cand_start:end_char]) > effective_overlap:
                    break
                back -= 1
            next_i = back
        i = next_i

    return packed


def _resolve_span_structure(
    text: str | None,
    structure: Mapping[str, Any] | None,
    start: int,
    end: int,
    *,
    fallback: TextChunk | None = None,
) -> tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...] | None]:
    if not structure:
        if fallback is not None:
            return fallback.pages, fallback.sheets, fallback.heading_path
        return (), (), None

    raw_blocks = structure.get("blocks")
    if not isinstance(raw_blocks, list):
        return (), (), None

    pages: set[int] = set()
    sheets: list[str] = []
    heading_path: tuple[str, ...] | None = None

    if text is not None and 0 <= start < len(text):
        loc = locate_offset(text, structure, start)
        if loc is not None and loc.heading_path:
            heading_path = loc.heading_path

    preceding_heading: tuple[int, tuple[str, ...]] | None = None
    for b in raw_blocks:
        if not isinstance(b, Mapping):
            continue
        b_start = b.get("start")
        b_end = b.get("end")
        if not isinstance(b_start, int) or not isinstance(b_end, int) or b_start >= b_end:
            continue
        b_hp = b.get("heading_path")
        if isinstance(b_hp, list) and b_hp and b_start <= start:
            hp_tuple = tuple(str(x) for x in b_hp)
            if preceding_heading is None or b_start >= preceding_heading[0]:
                preceding_heading = (b_start, hp_tuple)
        if b_start < end and start < b_end:
            pg = b.get("page")
            if isinstance(pg, int):
                pages.add(pg)
            if b.get("kind") == "sheet" and isinstance(b.get("name"), str):
                name = b["name"]
                if name not in sheets:
                    sheets.append(name)
            if heading_path is None and isinstance(b_hp, list) and b_hp:
                heading_path = tuple(str(x) for x in b_hp)

    if heading_path is None and preceding_heading is not None:
        heading_path = preceding_heading[1]
    if heading_path is None and fallback is not None:
        heading_path = fallback.heading_path

    return tuple(sorted(pages)), tuple(sheets), heading_path


def chunk_text(
    text: str,
    budget_tokens: int = 3_000,
    *,
    overlap_tokens: int = 0,
    structure: Mapping[str, Any] | None = None,
    locator: str | None = None,
    item: Item | None = None,
) -> tuple[TextChunk, ...]:
    """Split `text` into structure-aware `TextChunk` slices `<= budget_tokens`.

    Every returned `chunk` satisfies `chunk.text == text[chunk.start:chunk.end]` and
    `chunk.tokens <= budget_tokens` without silent truncation.
    """
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
        raise ValueError(f"budget_tokens must be a positive integer, got {budget_tokens!r}")
    if (
        isinstance(overlap_tokens, bool)
        or not isinstance(overlap_tokens, int)
        or overlap_tokens < 0
    ):
        raise ValueError(f"overlap_tokens must be a non-negative integer, got {overlap_tokens!r}")

    trimmed = _trim_span(text, 0, len(text))
    if trimmed is None:
        return ()

    s0, e0 = trimmed
    if estimate_tokens(text[s0:e0]) <= budget_tokens:
        pages, sheets, heading = _resolve_span_structure(text, structure, s0, e0)
        return (
            TextChunk(
                text=text[s0:e0],
                start=s0,
                end=e0,
                index=1,
                total=1,
                pages=pages,
                sheets=sheets,
                heading_path=heading,
                locator=locator,
                item=item,
                _structure=structure,
            ),
        )

    seeds = _structural_seed_spans(text, s0, e0, structure)
    atomic_spans: list[tuple[int, int]] = []
    for sec_s, sec_e in seeds:
        atomic_spans.extend(_split_span_to_budget(text, sec_s, sec_e, budget_tokens))

    packed_spans = _pack_spans(
        text,
        atomic_spans,
        budget_tokens=budget_tokens,
        overlap_tokens=overlap_tokens,
    )
    total = len(packed_spans)
    chunks: list[TextChunk] = []
    for idx, (cs, ce) in enumerate(packed_spans, start=1):
        pages, sheets, heading = _resolve_span_structure(text, structure, cs, ce)
        chunks.append(
            TextChunk(
                text=text[cs:ce],
                start=cs,
                end=ce,
                index=idx,
                total=total,
                pages=pages,
                sheets=sheets,
                heading_path=heading,
                locator=locator,
                item=item,
                _structure=structure,
            )
        )
    return tuple(chunks)


def chunk_item(
    item: Item,
    budget_tokens: int = 3_000,
    *,
    overlap_tokens: int = 0,
) -> tuple[TextChunk, ...]:
    """Split an `Item`'s extracted text into `TextChunk`s carrying `item` and block metadata."""
    if item.text is None:
        raise ValueError(f"{item.locator} has no text; declare text= on the stage")
    return chunk_text(
        item.text,
        budget_tokens=budget_tokens,
        overlap_tokens=overlap_tokens,
        structure=item.structure,
        locator=item.locator,
        item=item,
    )


def pack_batches(units: Sequence[str], sep: str, budget_tokens: int) -> list[str]:
    """Greedily join `units` using `sep` into batches `<= budget_tokens`."""
    if budget_tokens <= 0:
        raise ValueError(f"budget_tokens must be positive, got {budget_tokens}")
    packed: list[str] = []
    cur: list[str] = []
    for unit in units:
        if not unit:
            continue
        if not cur:
            cur.append(unit)
            continue
        candidate = sep.join([*cur, unit])
        if estimate_tokens(candidate) <= budget_tokens:
            cur.append(unit)
        else:
            packed.append(sep.join(cur))
            cur = [unit]
    if cur:
        packed.append(sep.join(cur))
    return packed


def reduce_tree[T](
    items: Sequence[T],
    *,
    budget_tokens: int,
    format_item: Callable[[int, int, T], str],
    reduce_batch: Callable[[str], T],
    sep: str = "\n",
) -> T:
    """Hierarchically fold `items` in token-bounded batches until a single result remains."""
    if not items:
        raise ValueError("reduce_tree() requires at least one item")
    if len(items) == 1:
        return items[0]

    effective_budget = max(1, budget_tokens)
    current = list(items)
    while len(current) > 1:
        total = len(current)
        lines = [format_item(idx, total, item) for idx, item in enumerate(current, start=1)]
        batches = pack_batches(lines, sep, effective_budget)
        if len(batches) == len(current):
            # Each formatted item already exceeds `effective_budget`; pair adjacent items so
            # every tree level strictly reduces `len(current)`.
            batches = [sep.join(lines[i : i + 2]) for i in range(0, len(lines), 2)]
        if len(batches) == 1:
            return reduce_batch(batches[0])
        current = [reduce_batch(batch_text) for batch_text in batches]
    return current[0]


# --- Verbatim & Fuzzy Quote Span Grounding (`rapidfuzz`) --------------------------


@dataclass(frozen=True)
class PreparedSource:
    """Pre-indexed folded text mapping every normalized character back to `original` offsets."""

    original: str
    lowered: str
    starts: list[int]
    ends: list[int]
    word_count: int


@dataclass(frozen=True)
class VerbatimMatch:
    """A verified quote slice in `source_text` with exact `[start, end)` character offsets."""

    text: str
    start: int
    end: int
    exact: bool
    score: float


def prepare_source(source_text: str) -> PreparedSource:
    """Fold whitespace, quotes, dashes, and ligatures while tracking exact source offsets."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []

    i = 0
    n = len(source_text)
    while i < n:
        ch = source_text[i]
        if ch.isspace():
            ws_start = i
            while i < n and source_text[i].isspace():
                i += 1
            if chars:
                chars.append(" ")
                starts.append(ws_start)
                ends.append(i)
            continue
        folded = _FOLD_MAP.get(ch, ch)
        for fc in folded:
            chars.append(fc.lower())
            starts.append(i)
            ends.append(i + 1)
        i += 1

    if chars and chars[-1] == " ":
        chars.pop()
        starts.pop()
        ends.pop()

    return PreparedSource(
        original=source_text,
        lowered="".join(chars),
        starts=starts,
        ends=ends,
        word_count=len(source_text.split()),
    )


def normalize_quote_query(raw_quote: str) -> str:
    """Normalize a model-returned quote for matching against `PreparedSource.lowered`."""
    cleaned = raw_quote.strip()
    for _ in range(3):
        prev = cleaned
        cleaned = re.sub(r"^(?:\.\.\.|…)+|(?:\.\.\.|…)+$", "", cleaned).strip()
        if (
            len(cleaned) >= 2
            and cleaned[0] in ('"', "'", "\u201c", "\u2018")
            and cleaned[-1] in ('"', "'", "\u201d", "\u2019")
        ):
            cleaned = cleaned[1:-1].strip()
        if cleaned == prev:
            break
    # Undo editorial first-letter bracketing like `"[T]he proposed rule"` -> `"the proposed rule"`.
    cleaned = re.sub(r"\[([A-Za-z])\]", lambda m: m.group(1), cleaned)

    out: list[str] = []
    in_ws = False
    for ch in cleaned:
        if ch.isspace():
            if out and not in_ws:
                out.append(" ")
                in_ws = True
            continue
        in_ws = False
        folded = _FOLD_MAP.get(ch, ch)
        for fc in folded:
            out.append(fc.lower())
    if out and out[-1] == " ":
        out.pop()
    return "".join(out)


def find_verbatim_span(
    raw_quote: str,
    source_text: str,
    *,
    prepared: PreparedSource | None = None,
    min_words: int | None = None,
    min_similarity: float = 92.0,
) -> VerbatimMatch | None:
    """Locate `raw_quote` in `source_text` and return its exact `[start, end)` slice.

    First attempts a folded exact match (`score=100.0, exact=True`). If that misses and
    `min_similarity < 100.0`, uses `rapidfuzz.fuzz.partial_ratio_alignment` to align
    near-verbatim quotes (such as single-character OCR or punctuation discrepancies) back
    to the exact substring in `source_text`. Paraphrased or fabricated quotes below
    `min_similarity` return `None`.
    """
    if not raw_quote or not source_text:
        return None
    prep = prepared if prepared is not None else prepare_source(source_text)
    if not prep.lowered:
        return None

    norm_query = normalize_quote_query(raw_quote)
    if not norm_query:
        return None

    query_words = norm_query.split()
    effective_min_words = (
        min_words
        if min_words is not None
        else (4 if prep.word_count >= 12 else max(2, prep.word_count // 2))
    )
    if len(query_words) < effective_min_words:
        return None

    pos = prep.lowered.find(norm_query)
    matched_len = len(norm_query)
    if pos == -1:
        trimmed = norm_query.rstrip(".,;:!?").strip()
        if trimmed and trimmed != norm_query and len(trimmed.split()) >= effective_min_words:
            pos = prep.lowered.find(trimmed)
            matched_len = len(trimmed)

    if pos != -1:
        end_pos = pos + matched_len - 1
        orig_start = prep.starts[pos]
        orig_end = prep.ends[end_pos]
        trimmed_span = _trim_span(prep.original, orig_start, orig_end)
        if trimmed_span is None:
            return None
        s, e = trimmed_span
        return VerbatimMatch(text=prep.original[s:e], start=s, end=e, exact=True, score=100.0)

    if min_similarity >= 100.0 or len(norm_query) < 20:
        return None

    alignment = fuzz.partial_ratio_alignment(
        norm_query,
        prep.lowered,
        score_cutoff=min_similarity,
    )
    if alignment is None:
        return None

    dest_start = alignment.dest_start
    dest_end = alignment.dest_end
    if dest_start < 0 or dest_end <= dest_start or dest_end > len(prep.lowered):
        return None

    # Guard against partial alignment matching only a short fragment of the query.
    span_len = dest_end - dest_start
    if not (0.85 * len(norm_query) <= span_len <= 1.18 * len(norm_query)):
        return None

    # Expand to whole token boundaries in `prep.lowered` so we never slice mid-word.
    while (
        dest_start > 0
        and prep.lowered[dest_start - 1].isalnum()
        and prep.lowered[dest_start].isalnum()
    ):
        dest_start -= 1
    while (
        dest_end < len(prep.lowered)
        and prep.lowered[dest_end - 1].isalnum()
        and prep.lowered[dest_end].isalnum()
    ):
        dest_end += 1

    orig_start = prep.starts[dest_start]
    orig_end = prep.ends[dest_end - 1]
    trimmed_span = _trim_span(prep.original, orig_start, orig_end)
    if trimmed_span is None:
        return None
    s, e = trimmed_span
    verbatim = prep.original[s:e]
    if len(verbatim.split()) < effective_min_words:
        return None
    return VerbatimMatch(
        text=verbatim,
        start=s,
        end=e,
        exact=False,
        score=float(alignment.score),
    )


def renumber_chunks(chunks: Sequence[TextChunk]) -> tuple[TextChunk, ...]:
    """Return `chunks` with `index` (`1..N`) and `total` (`N`) updated in sequence."""
    total = len(chunks)
    return tuple(replace(c, index=idx, total=total) for idx, c in enumerate(chunks, start=1))
