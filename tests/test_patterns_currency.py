"""`commissure.patterns.currency`: each rule on its input, and on the sibling it must not touch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from commissure.patterns import (
    Action,
    Claim,
    Lineage,
    Link,
    Node,
    deciding_actors,
    deciding_edge,
    graph_rows,
    lineage,
    lineage_verdict,
    merge_claims,
    resolve_currency,
    supersession_date,
)

D1, D2, D3 = date(2025, 3, 13), date(2025, 6, 13), date(2026, 6, 18)


def test_latest_dated_action_decides_and_undated_never_overrides_dated():
    c = resolve_currency(
        "45 CFR 1.1",
        [Action("A", "vacates", D2), Action("B", "stays", D1), Action("C", "supersedes")],
    )
    assert (c.status, c.effective_date, c.by, c.flags) == ("vacated", D2, "A", ())
    only_undated = resolve_currency("X", [Action("C", "supersedes"), Action("B", "stays")])
    assert (only_undated.status, only_undated.by, only_undated.flags) == (
        "superseded", "C", ("undated_action",),
    )  # fmt: skip
    assert [d.code for d in only_undated.diagnostics] == ["undated_action"]


def test_same_day_tie_goes_to_the_most_final_verb_and_is_flagged():
    # Source keys alone would pick "Z" (stays); finality picks the vacatur.
    c = resolve_currency("X", [Action("Z", "stays", D1), Action("A", "vacates", D1)])
    assert (c.status, c.by, c.flags) == ("vacated", "A", ("same_day_actions",))
    [diag] = c.diagnostics
    assert diag.code == "same_day_actions" and "A vacates" in diag.message
    assert "Z stays" in diag.message
    # Same verb on the same day: source key decides, still flagged.
    agree = resolve_currency("X", [Action("89 FR 1", "stays", D1), Action("PI-1", "stays", D1)])
    assert (agree.status, agree.by, agree.flags) == ("stayed", "PI-1", ("same_day_actions",))
    # A tie that is not on the deciding date is not flagged.
    earlier = resolve_currency(
        "X", [Action("A", "stays", D1), Action("B", "stays", D1), Action("C", "vacates", D2)]
    )
    assert (earlier.by, earlier.flags) == ("C", ())


def test_supersession_by_an_identity_not_held_is_external():
    c = resolve_currency("X", [Action("PI-9", "supersedes", D1)], source_held=lambda k: False)
    assert c.status == "superseded_external"
    held = resolve_currency("X", [Action("PI-9", "supersedes", D1)], source_held=lambda k: True)
    assert held.status == "superseded"
    stay = resolve_currency("X", [Action("PI-9", "stays", D1)], source_held=lambda k: False)
    assert stay.status == "stayed"  # only a supersession can be external


def test_freshness_stamp_never_outranks_the_action_and_is_flagged_only_when_later():
    later = resolve_currency("X", [Action("A", "vacates", D2)], freshness_date=D3)
    assert (later.status, later.flags) == ("vacated", ("freshness_stamp_contradicted",))
    earlier = resolve_currency("X", [Action("A", "vacates", D2)], freshness_date=D1)
    assert earlier.flags == ()


def test_amendments_mark_without_deciding_and_unknown_verbs_raise():
    c = resolve_currency("X", [Action("A", "amends", D1)])
    assert (c.status, c.amended, c.by) == (None, True, None)
    assert resolve_currency("X", []).status is None
    with pytest.raises(ValueError, match="unknown currency verb"):
        resolve_currency("X", [Action("A", "repeals", D1)])


def test_supersession_takes_the_superseding_identitys_date_then_its_own_document_date():
    assert supersession_date(D2, D1) == D2
    assert supersession_date(None, D1) == D1
    assert supersession_date(None, None) is None


@dataclass(frozen=True)
class Q:
    text: str


def test_merge_claims_keeps_every_file_and_quote_and_refuses_an_edge_without_evidence():
    rank = {"web": 0, "text": 1}
    edges = merge_claims(
        [
            Claim("A", "cites", "cfr", "X", ("a.pdf",), "text", Q("first")),
            Claim("A", "cites", "cfr", "X", ("a-web.pdf",), "web", Q("second"), D1),
            Claim("A", "cites", "cfr", "X", ("a.pdf",), "text", Q("first")),
            Claim("A", "cites", "cfr", "A", ("a.pdf",), "text", Q("self")),
            Claim("B", "stays", "cfr", "X", ("b.pdf",), "text", Q("stay"), D2),
        ],
        found_by_rank=rank,
    )
    assert [(e.source_key, e.kind, e.target_key) for e in edges] == [
        ("A", "cites", "X"),
        ("B", "stays", "X"),
    ]
    a = edges[0]
    assert a.source_files == ["a.pdf", "a-web.pdf"] and a.evidence == [Q("first"), Q("second")]
    assert (a.found_by, a.effective_date) == ("web", D1)
    assert a.fields()["source_files"] == ["a.pdf", "a-web.pdf"]
    with pytest.raises(ValueError, match="has no evidence"):
        merge_claims(
            [Claim("A", "cites", "cfr", "X", ("a.pdf",), "text", Q(""))], found_by_rank=rank
        )


def test_deciding_edge_is_the_currency_edge_from_the_decider():
    @dataclass
    class E:
        source_key: str
        kind: str
        target_key: str

    edges = [E("A", "cites", "X"), E("A", "vacates", "X"), E("B", "stays", "X")]
    assert deciding_edge(edges, "X", "A") == E("A", "vacates", "X")
    assert deciding_edge(edges, "X", None) is None
    assert deciding_edge(edges, "Y", "A") is None


def test_lineage_verdict_counts_the_same_day_as_at_issuance_and_never_guesses_a_date():
    assert lineage_verdict(D2, D1) == "superseded_at_issuance"
    assert lineage_verdict(D1, D1) == "superseded_at_issuance"
    assert lineage_verdict(D1, D2) == "superseded_after_issuance"
    # An unknown date cannot support an error, or rule one out.
    assert lineage_verdict(None, D1) == "lineage_undetermined"
    assert lineage_verdict(D1, None) == "lineage_undetermined"


@dataclass
class CE:
    source_key: str
    kind: str
    target_key: str
    effective_date: date | None


def test_deciding_actors_are_the_decider_and_its_same_day_co_actors():
    edges = [
        CE("89 FR 1", "stays", "X", D1),
        CE("PI-1", "stays", "X", D1),
        CE("IM-0", "stays", "X", date(2024, 1, 1)),  # earlier: not a decider
        CE("IM-2", "cites", "X", D1),  # not a currency action
        CE("PI-9", "stays", "Y", D1),  # another target
    ]
    assert deciding_actors(edges, "X", "PI-1", D1) == {"PI-1", "89 FR 1"}
    assert deciding_actors(edges, "X", None, None) == frozenset()


def test_lineage_never_faults_an_actor_and_traps_only_after_the_action():
    actors = frozenset({"PI-1", "89 FR 1"})
    stamp = ("freshness_stamp_contradicted",)
    # The non-deciding same-day co-actor citing what it stayed: exempt, no trap.
    assert lineage("89 FR 1", D2, action_date=D1, flags=stamp, actors=actors) == Lineage(
        None, False
    )
    # Cited after the action: the error, with the trap that explains it.
    assert lineage("IM-5", D2, action_date=D1, flags=stamp, actors=actors) == Lineage(
        "superseded_at_issuance", True
    )
    # Cited before the action: overtaken later; the later print could not mislead it.
    assert lineage("IM-4", D1, action_date=D2, flags=stamp, actors=actors) == Lineage(
        "superseded_after_issuance", False
    )
    # Unknown date: undetermined, never a trap.
    assert lineage("DRAFT", None, action_date=D1, flags=stamp, actors=actors) == Lineage(
        "lineage_undetermined", False
    )
    # No contradicted stamp: no trap.
    assert lineage("IM-5", D2, action_date=D1, flags=(), actors=actors).freshness_trap is False


@dataclass(frozen=True)
class FakeItem:
    key: str
    item_version_id: int


ITEMS = {"rule.pdf": FakeItem("item:1", 11), "im.pdf": FakeItem("item:2", 12),
         "att.pdf": FakeItem("item:3", 13)}  # fmt: skip


def test_graph_rows_project_entities_items_and_unresolved_targets_with_provenance():
    nodes = [
        Node("45 CFR 1.1", "cfr", ("rule.pdf",), {"held": "present"}),
        Node("IM-25-06", "acf_im", ("im.pdf",)),
        Node("45 CFR 1.2", "cfr", ("im.pdf",), {"held": "absent"}),
        Node("doc:att.pdf", None, ("att.pdf",)),
    ]
    links = [
        Link("vacates", "IM-25-06", "45 CFR 1.1", True, ("im.pdf",), "vacates 1.1"),
        Link("vacates", "IM-25-06", "45 CFR 1.2", False, ("im.pdf",)),
        Link("attachment_of", "doc:att.pdf", "IM-25-06", True, ("att.pdf",)),
    ]
    entities, edges = graph_rows(nodes, links, ITEMS)
    assert [(e.kind, e.key, e.item_version_ids) for e in entities] == [
        ("acf_im", "IM-25-06", (12,)),
        ("cfr", "45 CFR 1.1", (11,)),
        ("cfr", "45 CFR 1.2", (12,)),
    ]
    assert entities[1].attributes == {"held": "present"}
    held, absent, attach = edges
    assert (held.source_entity_key, held.target_entity_key) == (
        ("acf_im", "IM-25-06"), ("cfr", "45 CFR 1.1"),
    )  # fmt: skip
    assert (absent.target_entity_key, absent.target_unresolved) == (None, "45 CFR 1.2")
    assert (attach.source_item_key, attach.target_entity_key) == ("item:3", ("acf_im", "IM-25-06"))
    assert held.evidence == "vacates 1.1" and held.item_version_ids == (12,)


@pytest.mark.parametrize(
    ("nodes", "links", "message"),
    [
        ([Node("A", "cfr", ())], [], "names no file"),
        ([Node("A", "cfr", ("gone.pdf",))], [], "not items of this subject"),
        ([Node("A", "cfr", ("im.pdf",))], [Link("cites", "A", "B", True, ("im.pdf",))],
         "target 'B' names no node"),
        ([Node("A", "cfr", ("im.pdf",))], [Link("cites", "A", "B", False, ())],
         "not items of this subject"),
        ([Node("A", "cfr", ("im.pdf",)), Node("A", "cfr", ("im.pdf",))], [], "given twice"),
    ],
)  # fmt: skip
def test_graph_rows_refuse_a_graph_that_does_not_describe_its_inputs(nodes, links, message):
    with pytest.raises(ValueError, match=message):
        graph_rows(nodes, links, ITEMS)
