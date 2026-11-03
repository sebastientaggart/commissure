"""Tests for structure-aware chunking, adaptive subdivision, and rapidfuzz quote alignment."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from commissure.api import (
    EvidenceError,
    ExtractionFailed,
    Item,
    Source,
    chunk_item,
    chunk_text,
    find_verbatim_span,
    pack_batches,
    prepare_source,
    reduce_tree,
)
from commissure.evidence import resolve as resolve_evidence
from commissure.llm import TransportResponse
from tests.test_llm import CannedTransport, _make_seam


class ChunkFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    quote: str


def test_chunk_text_preserves_exact_slice_offsets_and_budget_across_all_split_levels():
    para_a = "Section I. Periodic Statements.\n\n" + (
        "The Bureau proposes to require monthly statements for closed-end residential mortgages. "
        * 8
    )
    para_b = (
        "Line one of multi-line block.\n"
        "Line two of multi-line block with clauses; clause two, clause three: clause four.\n"
        + ("Sentence one is short. Sentence two is also short! Sentence three asks why? " * 6)
    )
    unbroken = "X" * 320  # single 80-token unbroken token forcing character fallback
    source = f"  \n{para_a}\n\n{para_b}\n\n{unbroken}\n  "

    for budget in (120, 45, 25):
        chunks = chunk_text(source, budget_tokens=budget, overlap_tokens=8)
        assert len(chunks) > 1
        for idx, ch in enumerate(chunks, start=1):
            assert ch.index == idx
            assert ch.total == len(chunks)
            assert 0 <= ch.start < ch.end <= len(source)
            assert ch.text == source[ch.start : ch.end]
            assert ch.tokens <= budget
            assert not ch.text[0].isspace()
            assert not ch.text[-1].isspace()

        # Every non-whitespace character in source is covered by at least one chunk.
        covered = [False] * len(source)
        for ch in chunks:
            for pos in range(ch.start, ch.end):
                covered[pos] = True
        for pos, char in enumerate(source):
            if not char.isspace():
                assert covered[pos], f"non-whitespace char at {pos} dropped at budget={budget}"

    for bad_budget in (0, -5, True):
        with pytest.raises(ValueError, match="budget_tokens"):
            chunk_text("hello", budget_tokens=bad_budget)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="overlap_tokens"):
        chunk_text("hello", budget_tokens=10, overlap_tokens=-1)


def test_chunk_item_resolves_pages_headings_and_builds_verified_evidence_spans(tmp_path):
    p1 = "I. Small Servicer Scope\n\nSmall servicers originate and hold fewer than 1,000 loans."
    p2 = "II. Implementation Timeline\n\nManufactured home lenders need 24 months to comply."
    full_text = f"{p1}\n\n{p2}"
    p2_start = len(p1) + 2

    structure = {
        "blocks": [
            {"kind": "page", "start": 0, "end": len(p1), "page": 1, "flags": []},
            {
                "kind": "heading",
                "start": 0,
                "end": 23,
                "page": 1,
                "heading_path": ["I. Small Servicer Scope"],
            },
            {
                "kind": "page",
                "start": p2_start,
                "end": len(full_text),
                "page": 2,
                "flags": ["ocr"],
                "confidence": 91.5,
            },
            {
                "kind": "heading",
                "start": p2_start,
                "end": p2_start + 27,
                "page": 2,
                "heading_path": ["II. Implementation Timeline"],
            },
        ]
    }
    dummy_file = tmp_path / "comment.pdf"
    dummy_file.write_bytes(b"%PDF-1.4")

    item = Item(
        key="item:1",
        item_version_id=10,
        locator="C1/comment.pdf",
        role="comment",
        bytes_hash="abc123",
        size_bytes=8,
        path=dummy_file,
        source=Source(name="intake", uri=str(tmp_path)),
        text=full_text,
        structure=structure,
        parser="ocr",
        extracted_text_id=77,
    )

    chunks = chunk_item(item, budget_tokens=25)
    assert len(chunks) == 2
    assert chunks[0].pages == (1,)
    assert chunks[0].heading_path == ("I. Small Servicer Scope",)
    assert "file=C1/comment.pdf" in chunks[0].context_label()
    assert "page=1" in chunks[0].context_label()
    assert "section=I. Small Servicer Scope" in chunks[0].context_label()

    assert chunks[1].pages == (2,)
    assert chunks[1].heading_path == ("II. Implementation Timeline",)

    # Build an Evidence.span directly from chunk 2's relative offsets and resolve through core.
    target_phrase = "Manufactured home lenders need 24 months"
    rel_start = chunks[1].text.index(target_phrase)
    rel_end = rel_start + len(target_phrase)
    ev = chunks[1].span(rel_start, rel_end, kind="verbatim_quote")

    class RecordWithEv(BaseModel):
        model_config = ConfigDict(extra="forbid")

        ev: object

    from commissure.api import Evidence

    class TypedRecord(BaseModel):
        model_config = ConfigDict(extra="forbid")

        ev: Evidence

    doc, rows = resolve_evidence(TypedRecord(ev=ev), item)
    assert doc["ev"]["page"] == 2
    assert doc["ev"]["via_ocr"] is True
    assert doc["ev"]["confidence"] == pytest.approx(91.5)
    assert doc["ev"]["text"] == target_phrase
    assert len(rows) == 1

    # Out-of-bounds relative span is rejected immediately:
    with pytest.raises(ValueError, match="outside chunk"):
        chunks[1].span(-1, 5, kind="verbatim_quote")

    # Tampered evidence slice is rejected by core's resolve_evidence:
    bad_ev = ev.model_copy(update={"text": "tampered text"})
    with pytest.raises(EvidenceError, match="claims 'tampered text'"):
        resolve_evidence(TypedRecord(ev=bad_ev), item)


def test_find_verbatim_span_exact_and_rapidfuzz_near_match_preserve_true_source_slice():
    source = (
        "The Bureau’s proposed 12 C.F.R. § 1026.41(e)(4) exemption—covering small servicers "
        "that service 1,000 or fewer mortgage loans—should be expanded to cover error resolution "
        "and early intervention requirements."
    )
    prep = prepare_source(source)

    # 1. Exact match after folding smart quotes, em-dashes, and editorial [T]he bracketing:
    m_exact = find_verbatim_span(
        (
            "\"...[T]he Bureau's proposed 12 C.F.R. § 1026.41(e)(4) "
            'exemption-covering small servicers..."'
        ),
        source,
        prepared=prep,
    )
    assert m_exact is not None
    assert m_exact.exact is True
    assert m_exact.score == 100.0
    assert m_exact.text == source[m_exact.start : m_exact.end]
    assert m_exact.text.startswith("The Bureau’s proposed 12 C.F.R.")

    # 2. Near-verbatim match with minor OCR/model character discrepancies:
    #    Model wrote "1026.41(e)(4) exemptions covering small serv1cers that service 1000"
    #    (dropped punctuation/dash, "1" for "i", "1000" without comma).
    #    `find_verbatim_span` aligns via rapidfuzz and returns the TRUE verbatim slice.
    near_query = (
        "proposed 12 C.F.R. § 1026.41(e)(4) exemptions covering small serv1cers "
        "that service 1000 or fewer mortgage loans"
    )
    m_fuzzy = find_verbatim_span(
        near_query,
        source,
        prepared=prep,
        min_similarity=90.0,
    )
    assert m_fuzzy is not None
    assert m_fuzzy.exact is False
    assert m_fuzzy.score >= 90.0
    assert m_fuzzy.text == source[m_fuzzy.start : m_fuzzy.end]
    assert "small servicers" in m_fuzzy.text
    assert "1,000 or fewer mortgage loans" in m_fuzzy.text

    # 3. With min_similarity=100.0, the fuzzy match is refused:
    assert (
        find_verbatim_span(
            near_query,
            source,
            prepared=prep,
            min_similarity=100.0,
        )
        is None
    )

    # 4. Paraphrased or fabricated quote is rejected even with fuzzy matching enabled:
    assert (
        find_verbatim_span(
            "Small banks with under one thousand loans must be exempted from all servicing rules.",
            source,
            prepared=prep,
            min_similarity=90.0,
        )
        is None
    )


def test_extract_chunks_subdivides_on_truncation_and_input_too_large_without_tripping_breaker(ws):
    # Check that truncated JSON is flagged with `truncated=True`:
    from commissure.llm import coerce_and_validate

    with pytest.raises(ExtractionFailed) as trunc_exc:
        coerce_and_validate('{"topic": "servicing", "quote": "cut off mid str', ChunkFinding)
    assert trunc_exc.value.truncated is True

    with pytest.raises(ExtractionFailed) as non_trunc_exc:
        coerce_and_validate("not json at all without braces", ChunkFinding)
    assert non_trunc_exc.value.truncated is False

    # Build a 2-paragraph text where the combined chunk fails (simulating a reasoning loop +
    # truncated JSON retry), forcing `extract_chunks` to split it in half and succeed on each half.
    para_1 = "Paragraph one discusses periodic statements in detail. " * 6
    para_2 = "Paragraph two discusses force-placed insurance rules. " * 6
    full_text = f"{para_1.strip()}\n\n{para_2.strip()}"
    initial_chunks = chunk_text(full_text, budget_tokens=500)
    assert len(initial_chunks) == 1

    transport = CannedTransport(
        [
            # Parent chunk attempt 1: empty text after 58,000 reasoning tokens (truncated=True)
            TransportResponse(raw_text="", input_tokens=150, output_tokens=58_000),
            # Parent chunk retry attempt: cut off mid-JSON at max_tokens (truncated=True)
            TransportResponse(
                raw_text='{"topic": "combined", "quote": "Paragraph one',
                input_tokens=150,
                output_tokens=4096,
            ),
            # Sub-chunk 1 (para_1) succeeds:
            TransportResponse(
                raw_text=(
                    '{"topic": "periodic_statements", '
                    '"quote": "Paragraph one discusses periodic statements in detail."}'
                ),
                input_tokens=75,
                output_tokens=40,
            ),
            # Sub-chunk 2 (para_2) succeeds:
            TransportResponse(
                raw_text=(
                    '{"topic": "force_placed_insurance", '
                    '"quote": "Paragraph two discusses force-placed insurance rules."}'
                ),
                input_tokens=75,
                output_tokens=40,
            ),
        ]
    )
    # Set max_consecutive_failures=2: without rolling back the parent chunk's 2 failures on split,
    # the breaker would trip before the sub-chunks run!
    seam = _make_seam(
        ws,
        params={"max_consecutive_failures": 2},
        transport=transport,
    )
    results = seam.extract_chunks(
        ChunkFinding,
        initial_chunks,
        system="Extract topic and quote.",
        retry_params={"temperature": 0.2, "reasoning_effort": "low"},
        min_chunk_tokens=10,
    )
    assert len(results) == 2
    assert seam.breaker_error is None
    (c1, ext1), (c2, ext2) = results
    assert ext1.payload.topic == "periodic_statements"
    assert ext2.payload.topic == "force_placed_insurance"
    assert full_text[c1.start : c1.end] == c1.text
    assert full_text[c2.start : c2.end] == c2.text
    assert c1.start == 0
    assert c2.end == len(full_text)


def test_reduce_tree_folds_large_lists_in_bounded_batches():
    items = [f"Summary item {i} with several words of regulatory analysis." for i in range(1, 16)]
    batch_inputs: list[str] = []

    def format_item(idx: int, total: int, val: str) -> str:
        return f"[{idx}/{total}] {val}"

    def reduce_batch(batch_text: str) -> str:
        assert len(batch_text.encode("utf-8")) // 4 <= 60
        batch_inputs.append(batch_text)
        count = batch_text.count("[")
        return f"Reduced({count} items)"

    final = reduce_tree(
        items,
        budget_tokens=50,
        format_item=format_item,
        reduce_batch=reduce_batch,
    )
    assert final.startswith("Reduced(")
    assert len(batch_inputs) >= 4
    assert pack_batches(["a", "b", "c"], "\n", budget_tokens=50) == ["a\nb\nc"]
