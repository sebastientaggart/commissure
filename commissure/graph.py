"""The graph: entities and edges a stage emits beside its payload, and read-only queries.

Core owns the shape and provenance; recipes decide what kinds mean. A stage returns
`RecordEnvelope.valid(payload, entities=..., edges=...)` (or `.warning`); core indexes
them against the outcome that carries them, so `current_entities` and `current_edges`
follow the same snapshot rules as `current_outcomes` and a rerun supersedes them.

    EntitySpec(kind="person", key="p-17", attributes={"name": "..."})
    EdgeSpec(kind="mentions", source_item_key=item.key, target_entity_key=("person", "p-17"))
    EdgeSpec(kind="cites", source_item_key=item.key, target_unresolved="ref 12(b)")

An entity is named by `(kind, key)`: `key` is a stable natural key within its kind,
stored as `attributes["key"]`. An edge's entity endpoint must name an entity in the same
envelope, so a snapshot never points at a row a later run superseded; callers join
across outcomes by `(kind, key)`. Provenance names item versions the subject owns (an
item stage defaults to its own version); anything else fails the run, like evidence,
because it is a recipe bug, not bad data. An edge's `evidence` is free text the recipe
supplies and core does not check; it is not the core `Evidence` pointer.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from commissure.workspace import utc_now

if TYPE_CHECKING:
    from commissure.recipes import Group, Item

_ITEM_KEY = re.compile(r"item:[0-9]+")


class GraphError(Exception):
    """Entities or edges in an envelope do not check out against the stage's subject."""


class GraphFromWorker(RuntimeError):
    """`ctx.entities` or `ctx.edges` was called from a worker thread."""


def _item_key(value: str | None, field: str) -> None:
    if value is not None and not _ITEM_KEY.fullmatch(value):
        raise ValueError(f"{field} must look like 'item:<id>', got {value!r}")


class EntitySpec(BaseModel):
    """One entity a stage emits. `(kind, key)` names it within the envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    item_version_ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> EntitySpec:
        if "key" in self.attributes:
            raise ValueError("attributes['key'] is reserved; core stores EntitySpec.key there")
        try:
            json.dumps(self.attributes, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"attributes must be JSON-serializable ({exc})") from None
        return self


class EdgeSpec(BaseModel):
    """One edge. Exactly one source and exactly one target, as `edges` CHECKs require."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    source_entity_key: tuple[str, str] | None = None  # (kind, key)
    source_item_key: str | None = None  # item:<id>
    target_entity_key: tuple[str, str] | None = None
    target_item_key: str | None = None
    target_unresolved: str | None = Field(default=None, min_length=1)  # raw reference text
    weight: float | None = None
    evidence: str | None = None  # free text, unchecked; not the core Evidence pointer
    item_version_ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> EdgeSpec:
        if (self.source_entity_key is None) == (self.source_item_key is None):
            raise ValueError("an edge needs exactly one of source_entity_key, source_item_key")
        targets = (self.target_entity_key, self.target_item_key, self.target_unresolved)
        if sum(t is not None for t in targets) != 1:
            raise ValueError(
                "an edge needs exactly one of target_entity_key, target_item_key, target_unresolved"
            )
        _item_key(self.source_item_key, "source_item_key")
        _item_key(self.target_item_key, "target_item_key")
        if self.weight is not None and not math.isfinite(self.weight):
            raise ValueError("weight must be finite")
        return self


# --- indexing (main thread, inside the runner's per-subject transaction) ----------


def _members(subject_input: Item | Group) -> tuple[Item, ...]:
    return tuple(subject_input.items) if hasattr(subject_input, "items") else (subject_input,)


def _provenance(
    what: str, explicit: tuple[int, ...], default: tuple[int, ...], allowed: set[int]
) -> list[int]:
    ids = sorted(set(explicit or default))
    unknown = [i for i in ids if i not in allowed]
    if unknown:
        raise GraphError(
            f"{what} names item version(s) {unknown} the subject does not own"
            f" (owned: {sorted(allowed)})"
        )
    return ids


def index(
    conn: sqlite3.Connection,
    run_id: int,
    outcome_id: int,
    subject_input: Item | Group,
    entities: tuple[EntitySpec, ...],
    edges: tuple[EdgeSpec, ...],
) -> None:
    """Check and insert one envelope's graph. Raises `GraphError`, failing the run."""
    if not entities and not edges:
        return
    members = _members(subject_input)
    allowed = {m.item_version_id for m in members}
    # Only an item stage has an obvious owner; a group or run stage says which members.
    default = (subject_input.item_version_id,) if not hasattr(subject_input, "items") else ()
    now = utc_now()

    ids: dict[tuple[str, str], int] = {}
    for spec in entities:
        name = (spec.kind, spec.key)
        if name in ids:
            raise GraphError(f"entity {name} appears twice in one envelope")
        prov = _provenance(f"entity {name}", spec.item_version_ids, default, allowed)
        attributes = json.dumps(
            {**spec.attributes, "key": spec.key}, sort_keys=True, separators=(",", ":")
        )
        ids[name] = conn.execute(
            "INSERT INTO entities (kind, attributes, run_id, outcome_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (spec.kind, attributes, run_id, outcome_id, now),
        ).lastrowid
        conn.executemany(
            "INSERT INTO entity_provenance (entity_id, item_version_id) VALUES (?, ?)",
            [(ids[name], v) for v in prov],
        )

    def entity(key: tuple[str, str] | None, where: str) -> int | None:
        if key is None:
            return None
        if tuple(key) not in ids:
            raise GraphError(
                f"edge {where} {tuple(key)} names no entity in this envelope;"
                " emit the entity beside the edge"
            )
        return ids[tuple(key)]

    def item(key: str | None, where: str) -> int | None:
        if key is None:
            return None
        item_id = int(key.removeprefix("item:"))
        if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is None:
            raise GraphError(f"edge {where} {key} names no item in this workspace")
        return item_id

    for spec in edges:
        prov = _provenance(f"edge {spec.kind!r}", spec.item_version_ids, default, allowed)
        edge_id = conn.execute(
            "INSERT INTO edges (kind, source_entity_id, source_item_id, target_entity_id,"
            " target_item_id, target_unresolved, weight, evidence, run_id, outcome_id,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                spec.kind,
                entity(spec.source_entity_key, "source"),
                item(spec.source_item_key, "source"),
                entity(spec.target_entity_key, "target"),
                item(spec.target_item_key, "target"),
                spec.target_unresolved,
                spec.weight,
                spec.evidence,
                run_id,
                outcome_id,
                now,
            ),
        ).lastrowid
        conn.executemany(
            "INSERT INTO edge_provenance (edge_id, item_version_id) VALUES (?, ?)",
            [(edge_id, v) for v in prov],
        )


# --- queries (read only; current snapshot only) -----------------------------------


@dataclass(frozen=True)
class EntityRow:
    entity_id: int
    kind: str
    key: str
    attributes: dict[str, Any]  # without "key"
    item_version_ids: tuple[int, ...]
    stage: str
    subject_key: str
    outcome_id: int
    run_id: int
    stale: bool  # the emitting outcome is version, code, params, or input stale


@dataclass(frozen=True)
class EdgeRow:
    edge_id: int
    kind: str
    source_entity_key: tuple[str, str] | None
    source_item_key: str | None
    target_entity_key: tuple[str, str] | None
    target_item_key: str | None
    target_unresolved: str | None
    weight: float | None
    evidence: str | None
    item_version_ids: tuple[int, ...]
    stage: str
    subject_key: str
    outcome_id: int
    run_id: int
    stale: bool


_STALE = "(version_stale OR code_stale OR params_stale OR input_stale) AS stale"


def entities(
    conn: sqlite3.Connection, *, kind: str | None = None, key: str | None = None
) -> list[EntityRow]:
    """Current entities, optionally of one kind and/or natural key, in insertion order."""
    where, params = [], []
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if key is not None:
        where.append("key = ?")
        params.append(key)
    sql = (
        f"SELECT ce.*, {_STALE}, (SELECT json_group_array(p.item_version_id)"
        "  FROM entity_provenance p WHERE p.entity_id = ce.entity_id) AS prov"
        " FROM current_entities ce"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY entity_id"
    )
    out = []
    for r in conn.execute(sql, params):
        attributes = json.loads(r["attributes"])
        attributes.pop("key", None)
        out.append(
            EntityRow(
                entity_id=r["entity_id"],
                kind=r["kind"],
                key=r["key"],
                attributes=attributes,
                item_version_ids=tuple(sorted(json.loads(r["prov"]))),
                stage=r["stage"],
                subject_key=r["subject_key"],
                outcome_id=r["outcome_id"],
                run_id=r["run_id"],
                stale=bool(r["stale"]),
            )
        )
    return out


def _endpoint(side: str, value: tuple[str, str] | str, where: list[str], params: list) -> None:
    if isinstance(value, str):
        _item_key(value, f"{side}_key")
        where.append(f"{side}_item_key = ?")
        params.append(value)
    else:
        kind, key = value
        where.append(f"{side}_entity_kind = ? AND {side}_entity_key = ?")
        params += [kind, key]


def edges(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    source_key: tuple[str, str] | str | None = None,
    target_key: tuple[str, str] | str | None = None,
    target_unresolved: str | None = None,
) -> list[EdgeRow]:
    """Current edges. An endpoint key is `(kind, key)` for an entity or `'item:<id>'`."""
    where: list[str] = []
    params: list[Any] = []
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if source_key is not None:
        _endpoint("source", source_key, where, params)
    if target_key is not None:
        _endpoint("target", target_key, where, params)
    if target_unresolved is not None:
        where.append("target_unresolved = ?")
        params.append(target_unresolved)
    sql = (
        f"SELECT ce.*, {_STALE}, (SELECT json_group_array(p.item_version_id)"
        "  FROM edge_provenance p WHERE p.edge_id = ce.edge_id) AS prov"
        " FROM current_edges ce"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY edge_id"
    )

    def pair(r: sqlite3.Row, side: str) -> tuple[str, str] | None:
        k = r[f"{side}_entity_kind"]
        return None if k is None else (k, r[f"{side}_entity_key"])

    return [
        EdgeRow(
            edge_id=r["edge_id"],
            kind=r["kind"],
            source_entity_key=pair(r, "source"),
            source_item_key=r["source_item_key"],
            target_entity_key=pair(r, "target"),
            target_item_key=r["target_item_key"],
            target_unresolved=r["target_unresolved"],
            weight=r["weight"],
            evidence=r["evidence"],
            item_version_ids=tuple(sorted(json.loads(r["prov"]))),
            stage=r["stage"],
            subject_key=r["subject_key"],
            outcome_id=r["outcome_id"],
            run_id=r["run_id"],
            stale=bool(r["stale"]),
        )
        for r in conn.execute(sql, params)
    ]
