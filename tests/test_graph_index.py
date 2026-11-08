"""The graph: entities and edges emitted beside payloads, indexed per outcome.

A synthetic recipe written into `tmp_path` and driven through `commissure run`:
`tag` (item stage) emits a topic entity per item, an item -> topic edge, and an edge
whose target stays unresolved; `cluster` (group stage) emits one entity owning every
member and an edge to each member item. Its `mode` param breaks one thing on purpose.
"""

import json
import textwrap

import pytest
from pydantic import ValidationError

from commissure.api import EdgeSpec, EntitySpec, GraphFromWorker, RecordEnvelope, RunContext
from commissure.graph import edges, entities
from commissure.workspace import open_workspace

RECIPE = textwrap.dedent(
    """
    from pydantic import BaseModel, ConfigDict

    from commissure.api import EdgeSpec, EntitySpec, Group, Item, RecordEnvelope, RunContext, Stage


    class Tagged(BaseModel):
        model_config = ConfigDict(extra="forbid")
        locator: str


    class Cluster(BaseModel):
        model_config = ConfigDict(extra="forbid")
        size: int


    def tag(ctx: RunContext, item: Item):
        mode = ctx.params["mode"]
        stem = item.locator.rsplit(".", 1)[0]
        if mode == "quarantine_a" and stem == "a":
            return RecordEnvelope.quarantined("bad_input")
        topic = ("topic", f"{stem}-{ctx.params['suffix']}")
        prov = (item.item_version_id + 1000,) if mode == "bad_prov" else ()
        target = ("topic", "nowhere") if mode == "dangling" else topic
        return RecordEnvelope.valid(
            Tagged(locator=item.locator),
            entities=[EntitySpec(kind=topic[0], key=topic[1], attributes={"stem": stem},
                                 item_version_ids=prov)],
            edges=[
                EdgeSpec(kind="about", source_item_key=item.key, target_entity_key=target),
                EdgeSpec(kind="cites", source_item_key=item.key,
                         target_unresolved=f"ref {stem}", weight=0.5, evidence="footnote 1"),
            ],
        )


    def cluster(ctx: RunContext, group: Group):
        owned = tuple(i.item_version_id for i in group.items)
        if ctx.params["mode"] == "bad_prov":
            owned += (max(owned) + 1000,)
        return RecordEnvelope.valid(
            Cluster(size=len(group.items)),
            entities=[EntitySpec(kind="cluster", key=group.key, item_version_ids=owned)],
            edges=[
                EdgeSpec(kind="contains", source_entity_key=("cluster", group.key),
                         target_item_key=i.key, item_version_ids=(i.item_version_id,))
                for i in group.items
            ],
        )


    STAGES = [
        Stage("tag", tag, schema=Tagged, params={"mode": "ok", "suffix": "v1"}),
        Stage("cluster", cluster, schema=Cluster, subject="group", needs=("tag",),
              group_by=lambda item: "all", params={"mode": "ok"}),
    ]
    """
)


@pytest.fixture
def recipe(tmp_path):
    folder = tmp_path / "graphrecipe"
    folder.mkdir()
    (folder / "recipe.py").write_text(RECIPE, encoding="utf-8")
    return folder


@pytest.fixture
def ingested(cli, ws_root, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for stem in ("a", "b"):
        (src / f"{stem}.txt").write_text(f"text of {stem}", encoding="utf-8")
    assert cli.ingest(ws_root, src)[0] == 0
    return src


def run(cli, ws_root, recipe, step, *params):
    args = ["run", "--step", step, "--workspace", str(ws_root), "--recipe", str(recipe)]
    for p in params:
        args += ["--param", p]
    return cli(*args, "--json")


def q(ws_root, sql, *params):
    ws = open_workspace(ws_root)
    try:
        return [dict(r) for r in ws.conn.execute(sql, params)]
    finally:
        ws.close()


def current(ws_root):
    ws = open_workspace(ws_root)
    try:
        return entities(ws.conn), edges(ws.conn)
    finally:
        ws.close()


def item_ids(ws_root):
    return {
        r["locator"]: (r["item_id"], r["item_version_id"])
        for r in q(ws_root, "SELECT item_id, item_version_id, locator FROM current_items")
    }


# --- specs and envelopes -------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_unresolved": "x"},  # no source
        {"source_item_key": "item:1", "source_entity_key": ("t", "k"), "target_unresolved": "x"},
        {"source_item_key": "item:1"},  # no target
        {"source_item_key": "item:1", "target_item_key": "item:2", "target_unresolved": "x"},
        {"source_item_key": "item:1", "target_entity_key": ("t", "k"), "target_item_key": "item:2"},
        {"source_item_key": "1", "target_unresolved": "x"},  # not an item key
        {"source_item_key": "item:1", "target_unresolved": ""},
        {"source_item_key": "item:1", "target_unresolved": "x", "weight": float("nan")},
    ],
)
def test_edge_endpoints_mirror_the_table_checks(kwargs):
    with pytest.raises(ValidationError):
        EdgeSpec(kind="k", **kwargs)


def test_entity_key_is_reserved_and_attributes_must_be_json():
    with pytest.raises(ValidationError, match="reserved"):
        EntitySpec(kind="t", key="k", attributes={"key": "other"})
    with pytest.raises(ValidationError, match="JSON-serializable"):
        EntitySpec(kind="t", key="k", attributes={"when": object()})
    assert EntitySpec(kind="t", key="k", attributes={"n": 1}).attributes == {"n": 1}


def test_only_usable_outcomes_carry_a_graph():
    entity = EntitySpec(kind="t", key="k")
    assert RecordEnvelope.valid({"x": 1}, entities=[entity]).entities == (entity,)
    assert RecordEnvelope.warning({"x": 1}, "odd", entities=[entity]).entities == (entity,)
    for outcome in ("quarantined", "skipped"):
        with pytest.raises(ValidationError, match="no entities or edges"):
            RecordEnvelope(outcome=outcome, reason_code="r", entities=(entity,))


# --- indexing through real runs ------------------------------------------------------


def test_item_and_group_stages_populate_tables_provenance_and_current_views(
    cli, ws_root, recipe, ingested
):
    assert run(cli, ws_root, recipe, "tag").exit_code == 0
    assert run(cli, ws_root, recipe, "cluster").exit_code == 0
    ids = item_ids(ws_root)
    (a_item, a_ver), (b_item, b_ver) = ids["a.txt"], ids["b.txt"]

    assert q(ws_root, "SELECT count(*) AS n FROM entities")[0]["n"] == 3
    assert q(ws_root, "SELECT count(*) AS n FROM edges")[0]["n"] == 6
    # Every row is tied to the outcome that emitted it.
    assert q(ws_root, "SELECT count(*) AS n FROM entities WHERE outcome_id IS NULL")[0]["n"] == 0
    assert q(ws_root, "SELECT count(*) AS n FROM edges WHERE outcome_id IS NULL")[0]["n"] == 0

    ents, eds = current(ws_root)
    by_key = {(e.kind, e.key): e for e in ents}
    assert set(by_key) == {("topic", "a-v1"), ("topic", "b-v1"), ("cluster", "all")}
    assert by_key[("topic", "a-v1")].attributes == {"stem": "a"}
    assert by_key[("topic", "a-v1")].item_version_ids == (a_ver,)  # item-stage default
    assert by_key[("topic", "a-v1")].stage == "graphrecipe.tag"
    assert by_key[("topic", "a-v1")].subject_key == f"item:{a_item}"
    assert by_key[("cluster", "all")].item_version_ids == tuple(sorted((a_ver, b_ver)))
    assert by_key[("cluster", "all")].subject_key == "group:all"

    about = [e for e in eds if e.kind == "about"]
    assert {(e.source_item_key, e.target_entity_key) for e in about} == {
        (f"item:{a_item}", ("topic", "a-v1")),
        (f"item:{b_item}", ("topic", "b-v1")),
    }
    [cites_a] = [e for e in eds if e.kind == "cites" and e.source_item_key == f"item:{a_item}"]
    assert cites_a.target_unresolved == "ref a"
    assert cites_a.target_entity_key is None and cites_a.target_item_key is None
    assert (cites_a.weight, cites_a.evidence) == (0.5, "footnote 1")
    contains = [e for e in eds if e.kind == "contains"]
    assert {e.target_item_key for e in contains} == {f"item:{a_item}", f"item:{b_item}"}
    assert all(e.source_entity_key == ("cluster", "all") for e in contains)
    assert {e.item_version_ids for e in contains} == {(a_ver,), (b_ver,)}

    # Raw provenance tables agree with what the queries report.
    prov = q(ws_root, "SELECT item_version_id FROM entity_provenance ORDER BY item_version_id")
    assert [r["item_version_id"] for r in prov] == sorted([a_ver, b_ver, a_ver, b_ver])
    assert q(ws_root, "SELECT count(*) AS n FROM edge_provenance")[0]["n"] == 6

    # Filters.
    ws = open_workspace(ws_root)
    try:
        assert [e.key for e in entities(ws.conn, kind="topic", key="b-v1")] == ["b-v1"]
        assert entities(ws.conn, kind="cluster", key="b-v1") == []
        assert [e.kind for e in edges(ws.conn, source_key=("cluster", "all"))] == ["contains"] * 2
        assert [e.kind for e in edges(ws.conn, target_key=f"item:{a_item}")] == ["contains"]
        assert [e.source_item_key for e in edges(ws.conn, target_unresolved="ref b")] == [
            f"item:{b_item}"
        ]
        assert len(edges(ws.conn, kind="about", target_key=("topic", "a-v1"))) == 1
        with pytest.raises(ValueError, match="item:<id>"):
            edges(ws.conn, source_key="topic:a-v1")
    finally:
        ws.close()


def test_a_rerun_supersedes_the_previous_graph(cli, ws_root, recipe, ingested):
    assert run(cli, ws_root, recipe, "tag").exit_code == 0
    assert run(cli, ws_root, recipe, "cluster").exit_code == 0
    assert run(cli, ws_root, recipe, "tag", "suffix=v2").exit_code == 0

    ents, eds = current(ws_root)
    topics = sorted(e.key for e in ents if e.kind == "topic")
    assert topics == ["a-v2", "b-v2"]  # v1 rows are stored but no longer current
    assert q(ws_root, "SELECT count(*) AS n FROM entities WHERE kind = 'topic'")[0]["n"] == 4
    assert {e.target_entity_key[1] for e in eds if e.kind == "about"} == {"a-v2", "b-v2"}
    # The group stage did not rerun, so its graph stays current; the payloads it consumed
    # are unchanged (the rerun only renamed topics), so it is not input-stale either.
    [cluster] = [e for e in ents if e.kind == "cluster"]
    assert not cluster.stale

    # A subject whose latest outcome produced no payload has no current graph at all.
    assert run(cli, ws_root, recipe, "tag", "mode=quarantine_a", "suffix=v3").exit_code == 0
    ents, eds = current(ws_root)
    assert sorted(e.key for e in ents if e.kind == "topic") == ["b-v3"]
    a_item = item_ids(ws_root)["a.txt"][0]
    assert [e for e in eds if e.source_item_key == f"item:{a_item}"] == []


def test_a_group_rerun_supersedes_its_snapshot(cli, ws_root, recipe, ingested):
    assert run(cli, ws_root, recipe, "tag").exit_code == 0
    assert run(cli, ws_root, recipe, "cluster").exit_code == 0
    first = current(ws_root)[0]
    assert run(cli, ws_root, recipe, "cluster", "mode=again").exit_code == 0
    [cluster] = [e for e in current(ws_root)[0] if e.kind == "cluster"]
    [old] = [e for e in first if e.kind == "cluster"]
    assert cluster.outcome_id != old.outcome_id
    assert q(ws_root, "SELECT count(*) AS n FROM entities WHERE kind = 'cluster'")[0]["n"] == 2


@pytest.mark.parametrize(
    ("step", "mode", "message"),
    [
        ("tag", "bad_prov", "does not own"),
        ("tag", "dangling", "names no entity in this envelope"),
        ("cluster", "bad_prov", "does not own"),
    ],
)
def test_unowned_provenance_and_dangling_endpoints_fail_closed(
    cli, ws_root, recipe, ingested, step, mode, message
):
    if step == "cluster":
        assert run(cli, ws_root, recipe, "tag").exit_code == 0
    before = q(ws_root, "SELECT count(*) AS n FROM entities")[0]["n"]
    res = run(cli, ws_root, recipe, step, f"mode={mode}")
    assert res.exit_code == 1, res.output
    [failed] = q(ws_root, "SELECT * FROM runs WHERE step = ?", f"graphrecipe.{step}")[-1:]
    assert failed["status"] == "failed"
    assert message in failed["error"]
    # Nothing from the failing subject was kept: no graph rows, no outcome, no record.
    assert q(ws_root, "SELECT count(*) AS n FROM entities")[0]["n"] == before
    assert q(ws_root, "SELECT count(*) AS n FROM outcomes WHERE run_id = ?", failed["id"]) == [
        {"n": 0}
    ]


def test_an_edge_to_an_unknown_item_fails_closed(cli, ws_root, recipe, ingested):
    (recipe / "recipe.py").write_text(
        RECIPE.replace("target_item_key=i.key,", "target_item_key='item:9999',"),
        encoding="utf-8",
    )
    assert run(cli, ws_root, recipe, "tag").exit_code == 0
    res = run(cli, ws_root, recipe, "cluster")
    assert res.exit_code == 1
    [failed] = q(ws_root, "SELECT error FROM runs WHERE step = 'graphrecipe.cluster'")
    assert "names no item in this workspace" in failed["error"]


def test_graph_queries_from_a_worker_raise(cli, ws_root, recipe, ingested):
    assert run(cli, ws_root, recipe, "tag").exit_code == 0
    ws = open_workspace(ws_root)
    try:
        ctx = RunContext(0, "test", {}, workers=2, conn=ws.conn)
        assert len(ctx.entities(kind="topic")) == 2
        assert len(ctx.edges(kind="cites")) == 2
        with pytest.raises(GraphFromWorker):
            ctx.map(lambda _: ctx.entities(), [1, 2])
        with pytest.raises(GraphFromWorker):
            ctx.map(lambda _: ctx.edges(), [1, 2])
    finally:
        ws.close()


def test_inspect_reports_the_current_graph(cli, ws_root, recipe, ingested):
    assert run(cli, ws_root, recipe, "tag").exit_code == 0
    assert run(cli, ws_root, recipe, "tag", "suffix=v2").exit_code == 0
    res = cli("inspect", "--workspace", str(ws_root), "--json")
    assert res.exit_code == 0, res.stderr
    [report] = json.loads(res.stdout)["workspaces"]
    assert report["counts"]["entities"] == 4
    assert report["counts"]["current_entities"] == 2
    assert report["counts"]["current_edges"] == 4
    assert report["graph"] == [
        {"stage": "graphrecipe.tag", "table": "entity", "kind": "topic", "n": 2,
         "unresolved": None, "stale": 0},
        {"stage": "graphrecipe.tag", "table": "edge", "kind": "about", "n": 2,
         "unresolved": 0, "stale": 0},
        {"stage": "graphrecipe.tag", "table": "edge", "kind": "cites", "n": 2,
         "unresolved": 2, "stale": 0},
    ]  # fmt: skip
    human = cli("inspect", "--workspace", str(ws_root))
    assert "Current graph (3)" in human.stdout
