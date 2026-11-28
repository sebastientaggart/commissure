"""Currency of authorities: which actions supersede, stay, or vacate them, and when.

A corpus of authorities (regulations, rules, guidance issuances, any instrument that later
instruments act on) answers "is this still in effect, and since when?" from dated actions
that point at it. The rules here are domain-neutral; a recipe decides what its identities,
verbs' evidence, and families are, and keeps its own payload schemas:

- ``resolve_currency``: the latest dated action decides; an undated one only when no dated
  one exists; on a same-day tie the most final verb wins and the tie is flagged; a
  freshness stamp ("up to date as of") never outranks an action.
- ``supersession_date``: a supersession takes effect on the superseding identity's date.
- ``merge_claims``: one edge per (source, kind, target), every file and quote kept, and no
  edge without evidence.
- ``lineage``: whether citing a no-longer-current authority was already wrong when the
  citing document was issued, became wrong later, or cannot be placed; never for a document
  that is itself a deciding actor; with the freshness-stamp trap only where it can mislead.
- ``graph_rows``: the same graph as core ``EntitySpec`` / ``EdgeSpec`` rows with provenance.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from commissure.graph import EdgeSpec, EntitySpec
from commissure.records import Diagnostic

if TYPE_CHECKING:
    from commissure.recipes import Item

VERB_STATUS: dict[str, str] = {"supersedes": "superseded", "stays": "stayed", "vacates": "vacated"}
# On a same-day tie the most final action decides: a vacatur ends the authority outright, a
# supersession replaces it, a stay only suspends it.
FINALITY: dict[str, int] = {"stays": 0, "supersedes": 1, "vacates": 2}
CURRENCY_VERBS = frozenset(VERB_STATUS)
AMENDS = "amends"


# --- currency resolution -------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One currency claim on a target: ``source_key`` ``verb`` it, effective on a date."""

    source_key: str
    verb: str  # supersedes, stays, vacates, or amends
    effective_date: date | None = None


@dataclass(frozen=True)
class Currency:
    """What a target's incoming actions decide. ``status`` is None when no currency action
    (only amendments, or nothing) points at it: the caller keeps the identity's own status."""

    status: str | None
    effective_date: date | None
    by: str | None
    amended: bool
    flags: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


def _order(a: Action) -> tuple[int, str]:
    return FINALITY[a.verb], a.source_key


def resolve_currency(
    key: str,
    actions: Iterable[Action],
    *,
    freshness_date: date | None = None,
    source_held: Callable[[str], bool] = lambda _key: True,
) -> Currency:
    """Decide ``key``'s status from the actions that point at it.

    - The latest dated action decides. An undated action decides only when no dated one
      exists (flag ``undated_action``): an action of unknown date cannot be ordered after
      one of known date.
    - Actions dated the same day as the decider are ordered by finality (vacates over
      supersedes over stays), then by source key, and the tie is flagged
      ``same_day_actions`` with every action named, so a reviewer sees it.
    - A supersession by an identity the corpus does not hold (``source_held``) is
      ``superseded_external``.
    - A freshness stamp later than the deciding action's date is flagged
      ``freshness_stamp_contradicted``; the action still decides.
    """
    actions = list(actions)
    unknown = sorted({a.verb for a in actions} - CURRENCY_VERBS - {AMENDS})
    if unknown:
        raise ValueError(f"{key}: unknown currency verb(s) {unknown}")
    amended = any(a.verb == AMENDS for a in actions)
    currency = [a for a in actions if a.verb in CURRENCY_VERBS]
    if not currency:
        return Currency(status=None, effective_date=None, by=None, amended=amended)

    dated = [a for a in currency if a.effective_date is not None]
    flags: list[str] = []
    diags: list[Diagnostic] = []
    if dated:
        decider = max(dated, key=lambda a: (a.effective_date, *_order(a)))
        same_day = sorted(
            (a for a in dated if a.effective_date == decider.effective_date), key=_order
        )
        if len(same_day) > 1:
            flags.append("same_day_actions")
            named = ", ".join(f"{a.source_key} {a.verb}" for a in same_day)
            diags.append(
                Diagnostic(
                    code="same_day_actions",
                    field=key,
                    message=f"{key}: {named} on {decider.effective_date};"
                    f" {decider.source_key} decides (most final, then source key)",
                )
            )
    else:
        decider = max(currency, key=_order)
        flags.append("undated_action")
        diags.append(
            Diagnostic(
                code="undated_action",
                field=key,
                message=f"{key}: only undated actions; applied the last",
            )
        )

    status = VERB_STATUS[decider.verb]
    if status == "superseded" and not source_held(decider.source_key):
        status = "superseded_external"
    when = decider.effective_date
    if freshness_date and when and freshness_date > when:
        flags.append("freshness_stamp_contradicted")
        diags.append(
            Diagnostic(
                code="freshness_stamp_contradicted",
                field=key,
                message=f"{key} says up to date as of {freshness_date}; "
                f"{status} effective {when} by {decider.source_key}",
            )
        )
    return Currency(
        status=status,
        effective_date=when,
        by=decider.source_key,
        amended=amended,
        flags=tuple(flags),
        diagnostics=tuple(diags),
    )


def supersession_date(identity_date: date | None, source_date: date | None) -> date | None:
    """A supersession takes effect on the superseding identity's own (reconciled) date, or,
    when it has none, on a date the extractor read from the superseding document itself (a
    rule's publication date). Never the sentence's own date: "supersedes PI-24-07 issued
    May 21, 2024" dates the superseded issuance, not the supersession."""
    return identity_date or source_date


# --- edges ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One relationship read from one file: ``source_key`` ``kind`` ``target_key``."""

    source_key: str
    kind: str
    target_kind: str
    target_key: str
    files: tuple[str, ...]
    found_by: str
    evidence: Any  # the recipe's quote: anything with a `.text`; equal quotes are merged
    effective_date: date | None = None


@dataclass
class MergedEdge:
    source_key: str
    kind: str
    target_kind: str
    target_key: str
    effective_date: date | None
    found_by: str
    source_files: list[str] = field(default_factory=list)
    evidence: list[Any] = field(default_factory=list)

    def fields(self) -> dict[str, Any]:
        """The edge as keyword arguments for the recipe's own edge schema."""
        return {
            "source_key": self.source_key,
            "source_files": list(self.source_files),
            "kind": self.kind,
            "target_kind": self.target_kind,
            "target_key": self.target_key,
            "effective_date": self.effective_date,
            "found_by": self.found_by,
            "evidence": list(self.evidence),
        }


def merge_claims(claims: Iterable[Claim], *, found_by_rank: Mapping[str, int]) -> list[MergedEdge]:
    """One edge per (source, kind, target), in first-seen order.

    Files and quotes from every claim are kept (renditions each add theirs); the most
    trusted ``found_by`` wins (lowest rank); the first known effective date is kept. A
    claim from an identity to itself is dropped. An edge whose quotes are all empty raises
    ``ValueError``: it would be a relationship nobody can check.
    """
    merged: dict[tuple[str, str, str], MergedEdge] = {}
    for c in claims:
        if c.source_key == c.target_key:
            continue
        edge = merged.setdefault(
            (c.source_key, c.kind, c.target_key),
            MergedEdge(
                source_key=c.source_key,
                kind=c.kind,
                target_kind=c.target_kind,
                target_key=c.target_key,
                effective_date=c.effective_date,
                found_by=c.found_by,
            ),
        )
        for f in c.files:
            if f not in edge.source_files:
                edge.source_files.append(f)
        if found_by_rank[c.found_by] < found_by_rank[edge.found_by]:
            edge.found_by = c.found_by
        if edge.effective_date is None:
            edge.effective_date = c.effective_date
        if c.evidence.text and c.evidence not in edge.evidence:
            edge.evidence.append(c.evidence)
    for edge in merged.values():
        if not edge.evidence:
            raise ValueError(
                f"edge {edge.source_key} {edge.kind} {edge.target_key} has no evidence"
            )
    return list(merged.values())


def deciding_edge(edges: Iterable[Any], target_key: str, by: str | None) -> Any | None:
    """The currency edge from ``by`` to ``target_key`` (the one that decided its status)."""
    if not by:
        return None
    return next(
        (
            e
            for e in edges
            if e.target_key == target_key and e.source_key == by and e.kind in CURRENCY_VERBS
        ),
        None,
    )


# --- lineage -------------------------------------------------------------------------------

LineageVerdict = Literal[
    "superseded_at_issuance", "superseded_after_issuance", "lineage_undetermined"
]


def lineage_verdict(citing_date: date | None, action_date: date | None) -> LineageVerdict:
    """Citing an authority that an action ended: already wrong when the citing document was
    issued (the action took effect on or before its date), only wrong later, or, when either
    date is unknown, undetermined. An unknown date is never read as either: nothing places
    the citation before or after the action, so it cannot support an error."""
    if citing_date is None or action_date is None:
        return "lineage_undetermined"
    if action_date <= citing_date:
        return "superseded_at_issuance"
    return "superseded_after_issuance"


def deciding_actors(
    edges: Iterable[Any], target_key: str, by: str | None, effective_date: date | None
) -> frozenset[str]:
    """The identities whose own action on ``target_key`` took effect on its deciding date:
    the decider ``by``, plus any same-day co-actor (`same_day_actions`). Citing an authority
    you are acting on is the act, not reliance on it, so lineage never faults an actor."""
    if not by:
        return frozenset()
    same_day = {
        e.source_key
        for e in edges
        if e.target_key == target_key
        and e.kind in CURRENCY_VERBS
        and e.effective_date == effective_date
    }
    return frozenset({by, *same_day})


@dataclass(frozen=True)
class Lineage:
    """What citing a no-longer-current authority means for one citing document. ``verdict``
    is None when the document is one of the deciding actors."""

    verdict: LineageVerdict | None
    freshness_trap: bool


def lineage(
    citing_key: str,
    citing_date: date | None,
    *,
    action_date: date | None,
    flags: Iterable[str],
    actors: frozenset[str],
) -> Lineage:
    """Lineage for a citation of an authority whose status an action decided.

    The caller has already decided the authority is no longer current. A deciding actor is
    exempt (``deciding_actors``). The freshness-stamp trap (`freshness_stamp_contradicted`:
    the held print claims to be current but predates the action) applies only when the
    citation came after the action, the one case a drafter could have been misled by such a
    print; it explains the `superseded_at_issuance` error rather than repeating it, so the
    recipe reports it as a warning.
    """
    if citing_key in actors:
        return Lineage(verdict=None, freshness_trap=False)
    verdict = lineage_verdict(citing_date, action_date)
    trap = "freshness_stamp_contradicted" in flags and verdict == "superseded_at_issuance"
    return Lineage(verdict=verdict, freshness_trap=trap)


# --- core graph rows -----------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """An identity as a core row. ``kind`` None makes it an item endpoint: a one-file
    document (an attachment, a report) whose edges start or end at its file."""

    key: str
    kind: str | None
    files: tuple[str, ...]  # locators it is read from: its provenance
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Link:
    """An edge as a core row. ``held`` False keeps the target as ``target_unresolved`` (its
    key), so an authority not in the intake joins its entity on the key string."""

    kind: str
    source_key: str
    target_key: str
    held: bool
    files: tuple[str, ...]
    evidence: str | None = None


def graph_rows(
    nodes: Iterable[Node], links: Iterable[Link], items: Mapping[str, Item]
) -> tuple[list[EntitySpec], list[EdgeSpec]]:
    """Project nodes and links to core rows, entities sorted by ``(kind, key)``.

    Every row's provenance is the item versions of its files, and must be non-empty, as
    core requires of a group or run stage. A link endpoint that names no node, a node
    without files, or a file not in ``items`` raises ``ValueError``: the recipe built a
    graph that does not describe its inputs.
    """
    by_key: dict[str, Node] = {}
    for n in nodes:
        if n.key in by_key:
            raise ValueError(f"node {n.key!r} given twice")
        if not n.files:
            raise ValueError(f"node {n.key!r} names no file")
        by_key[n.key] = n

    def versions(files: Sequence[str], what: str) -> tuple[int, ...]:
        missing = [f for f in files if f not in items]
        if missing or not files:
            raise ValueError(f"{what}: files {missing or '[]'} are not items of this subject")
        return tuple(sorted({items[f].item_version_id for f in files}))

    def endpoint(side: str, key: str) -> dict[str, Any]:
        node = by_key.get(key)
        if node is None:
            raise ValueError(f"{side} {key!r} names no node")
        if node.kind is None:
            return {f"{side}_item_key": items[node.files[0]].key}
        return {f"{side}_entity_key": (node.kind, node.key)}

    entities = [
        EntitySpec(
            kind=n.kind,
            key=n.key,
            attributes=dict(n.attributes),
            item_version_ids=versions(n.files, f"node {n.key!r}"),
        )
        for n in sorted(by_key.values(), key=lambda n: (n.kind or "", n.key))
        if n.kind is not None
    ]
    edges = [
        EdgeSpec(
            kind=link.kind,
            **endpoint("source", link.source_key),
            **(
                endpoint("target", link.target_key)
                if link.held
                else {"target_unresolved": link.target_key}
            ),
            evidence=link.evidence,
            item_version_ids=versions(link.files, f"edge {link.kind} {link.source_key}"),
        )
        for link in links
    ]
    return entities, edges
