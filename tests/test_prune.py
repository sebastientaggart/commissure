"""`commissure prune`: clear superseded unreferenced `extracted_text` bodies."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

from commissure import review
from commissure.parse import PARSERS, Parser
from commissure.parse.model import ParsedDoc
from commissure.recipes import load_recipe, recipe_plan, register_stages
from commissure.serve import InterfaceApp
from commissure.workspace import DB_NAME, open_workspace, transaction

DEMO = Path(__file__).parent / "recipes" / "demo"

HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from commissure.workspace import lock

    with lock(Path(sys.argv[1])):
        print("locked", flush=True)
        sys.stdin.readline()
    """
)


def q(ws_root: Path, sql: str, *params) -> list[dict]:
    ws = open_workspace(ws_root)
    try:
        return [dict(r) for r in ws.conn.execute(sql, params)]
    finally:
        ws.close()


def _fake_parser(base: Parser, tag: str, extra_token: str = "") -> Parser:
    def _fn(path: Path, limits) -> ParsedDoc:
        doc = base.parse(path, limits)
        if not extra_token:
            return doc
        suffix = f"\n{extra_token}"
        text = doc.text + suffix
        blocks = list(doc.blocks)
        if blocks:
            blocks[-1] = replace(blocks[-1], end=len(text))
        return ParsedDoc(
            text=text,
            blocks=blocks,
            tables=doc.tables,
            metadata={**doc.metadata, "tag": tag},
            issues=doc.issues,
        )

    return Parser(
        base.name,
        base.extensions,
        _fn,
        base.modules,
        base.libraries,
        stdlib=base.stdlib,
        external=lambda: f"test={tag}",
    )


def test_prune_clears_superseded_bodies_and_preserves_equivalences(
    cli, ws_root, tmp_path, monkeypatch
):
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.json").write_text(json.dumps({"name": "alpha", "amount": 10}), encoding="utf-8")
    (src / "beta.json").write_text(json.dumps({"name": "beta", "amount": 25}), encoding="utf-8")
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "table.csv").write_text("col1,col2\nval1,val2\n", encoding="utf-8")

    recipe_dir = tmp_path / "demo"
    recipe_dir.mkdir()
    (recipe_dir / "recipe.py").write_text(
        textwrap.dedent(
            """
            import json
            from pydantic import BaseModel, ConfigDict, Field
            from commissure.api import Check, Diagnostic, Evidence, RecordEnvelope, Stage

            class Entry(BaseModel):
                model_config = ConfigDict(extra="forbid")
                name: str
                amount: int
                evidence: list[Evidence] = Field(min_length=1)

            class Summary(BaseModel):
                model_config = ConfigDict(extra="forbid")
                total: int
                review_lines: list[str]

            def extract(ctx, item):
                data = json.loads(item.text)
                name = data["name"]
                at = item.text.index(name)
                ev = Evidence.span(item, at, at + len(name), kind="name")
                return Entry(name=name, amount=data["amount"], evidence=[ev])

            def summarize(ctx, group):
                if not group.items:
                    return RecordEnvelope.skipped("empty", Diagnostic(code="empty"))
                tot = sum(i.upstream["extract"].amount for i in group.items)
                return Summary(total=tot, review_lines=ctx.review_report()["lines"])

            STAGES = [
                Stage(
                    "extract",
                    extract,
                    schema=Entry,
                    text="default",
                    roles=("corpus",),
                    checks=(Check("/evidence/*", "Does the name match?"),),
                ),
                Stage(
                    "summarize",
                    summarize,
                    schema=Summary,
                    subject="run",
                    roles=("corpus",),
                    needs=("extract",),
                    reads_review=True,
                ),
            ]
            """
        ),
        encoding="utf-8",
    )

    assert cli.ingest(ws_root, src)[0] == 0
    assert cli.ingest(ws_root, ref, "--role", "reference")[0] == 0

    # Parse v1 with a token that only exists in the superseded parse.
    orig_json = PARSERS["json"]
    orig_csv = PARSERS["csv"]
    monkeypatch.setitem(
        PARSERS, "json", _fake_parser(orig_json, "v1", "superseded_v1_unique_token")
    )
    monkeypatch.setitem(PARSERS, "csv", _fake_parser(orig_csv, "v1", "superseded_csv_token"))
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0

    # Modify alpha.json and re-ingest + parse under v1 so drift has a 'content' change_kind pair.
    (src / "alpha.json").write_text(json.dumps({"name": "alpha", "amount": 15}), encoding="utf-8")
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0

    # Now upgrade parsers to v2 (current) and re-parse.
    monkeypatch.setitem(PARSERS, "json", orig_json)
    monkeypatch.setitem(PARSERS, "csv", orig_csv)
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0

    # Run recipe stages and draw a review sample + verdict, then check reads_review stage.
    assert (
        cli(
            "run", "--step", "extract", "--workspace", str(ws_root), "--recipe", str(recipe_dir)
        ).exit_code
        == 0
    )
    s_res = cli(
        "review",
        "sample",
        "--workspace",
        str(ws_root),
        "--recipe",
        str(recipe_dir),
        "--check",
        "extract:/evidence/*",
        "--n",
        "1",
        "--seed",
        "7",
        "--reviewer",
        "tester",
        "--json",
    )
    assert s_res.exit_code == 0, s_res.output
    assert (
        cli(
            "review",
            "record",
            "1",
            "correct",
            "--workspace",
            str(ws_root),
            "--reviewer",
            "tester",
        ).exit_code
        == 0
    )
    assert (
        cli(
            "run", "--step", "summarize", "--workspace", str(ws_root), "--recipe", str(recipe_dir)
        ).exit_code
        == 0
    )

    # Before prune: superseded FTS token is still in the FTS index.
    assert q(
        ws_root,
        "SELECT rowid FROM extracted_text_fts WHERE extracted_text_fts MATCH ?",
        '"superseded_v1_unique_token"',
    )

    def snapshot():
        ws = open_workspace(ws_root)
        try:
            loaded = load_recipe(recipe_dir)
            register_stages(ws.conn, loaded)
            summarize_fp = [s.input_fp for s in recipe_plan(ws, loaded, "summarize").subjects()]
            return {
                "current_text": [
                    dict(r)
                    for r in ws.conn.execute("SELECT * FROM current_text ORDER BY item_id, parser")
                ],
                "drift": [
                    dict(r) for r in ws.conn.execute("SELECT * FROM drift ORDER BY item_version_id")
                ],
                "current_outcomes": [
                    dict(r)
                    for r in ws.conn.execute("SELECT * FROM current_outcomes ORDER BY outcome_id")
                ],
                "evidence_review": [
                    dict(r)
                    for r in ws.conn.execute(
                        "SELECT * FROM evidence_review ORDER BY outcome_id, evidence_id, page"
                    )
                ],
                "review_samples": [
                    dict(r) for r in ws.conn.execute("SELECT * FROM review_samples ORDER BY id")
                ],
                "review_instances": [
                    dict(r) for r in ws.conn.execute("SELECT * FROM review_instances ORDER BY id")
                ],
                "verdicts": [
                    dict(r) for r in ws.conn.execute("SELECT * FROM verdicts ORDER BY id")
                ],
                "review_report": review.report(ws.conn),
                "summarize_fp": summarize_fp,
                "search_alpha": json.loads(
                    cli("search", "alpha", "--workspace", str(ws_root), "--json").stdout
                ),
            }
        finally:
            ws.close()

    before = snapshot()

    # Prune superseded bodies.
    p_res = cli("prune", "--workspace", str(ws_root), "--json")
    assert p_res.exit_code == 0, p_res.output
    report = json.loads(p_res.stdout)
    # Current item versions (alpha v2, beta v1, table.csv v1) each had a v1 and v2 parse:
    # 3 superseded rows total (2 json, 1 csv). Note: alpha's item_version 1 only had v1 parse,
    # so its v1 row is the newest for (item_version 1, json) and stays unpruned.
    assert report["dry_run"] is False
    assert report["pruned_rows"] == 3
    assert report["pruned_bytes"] > 0
    assert [p["parser"] for p in report["by_parser"]] == ["csv", "json"]
    assert {p["parser"]: p["rows"] for p in report["by_parser"]} == {"csv": 1, "json": 2}

    rows = q(
        ws_root,
        "SELECT id, item_version_id, parser, parser_version, text, structure, text_hash, pruned_at"
        " FROM extracted_text ORDER BY id",
    )
    newest_ids = {
        r["id"]
        for r in q(
            ws_root,
            "SELECT max(id) AS id FROM extracted_text GROUP BY item_version_id, parser",
        )
    }
    for r in rows:
        if r["id"] in newest_ids:
            assert r["pruned_at"] is None
            assert len(r["text"]) > 0
        else:
            assert r["pruned_at"] is not None
            assert r["text"] == ""
            assert r["structure"] == "{}"
            assert len(r["text_hash"]) == 64

    # Superseded FTS tokens on pruned rows are gone from extracted_text_fts.
    assert (
        q(
            ws_root,
            "SELECT rowid FROM extracted_text_fts WHERE extracted_text_fts MATCH ?",
            '"superseded_csv_token"',
        )
        == []
    )
    pruned_ids = {r["id"] for r in rows if r["id"] not in newest_ids}
    fts_hits = {
        r["rowid"]
        for r in q(
            ws_root,
            "SELECT rowid FROM extracted_text_fts WHERE extracted_text_fts MATCH ?",
            '"superseded_v1_unique_token"',
        )
    }
    assert fts_hits.isdisjoint(pruned_ids)
    assert (
        json.loads(
            cli(
                "search",
                "superseded_v1_unique_token",
                "--workspace",
                str(ws_root),
                "--json",
            ).stdout
        )["hits"]
        == []
    )

    # Full equivalence of views, staleness flags, review tables, and reads_review stage input_fp.
    after = snapshot()
    assert after == before


def test_prune_preserves_superseded_row_named_by_evidence(cli, ws_root, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "cited.json").write_text(
        json.dumps({"name": "cited_item", "amount": 10}), encoding="utf-8"
    )
    (src / "uncited.json").write_text(json.dumps({"name": "other", "amount": 20}), encoding="utf-8")
    assert cli.ingest(ws_root, src)[0] == 0

    orig_json = PARSERS["json"]
    monkeypatch.setitem(PARSERS, "json", _fake_parser(orig_json, "v1", "v1_tail"))
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0

    # Cite records evidence pointing at cited.json's v1 extracted_text row; uncited.json is skipped.
    c_res = cli(
        "run",
        "--step",
        "cite",
        "--workspace",
        str(ws_root),
        "--recipe",
        str(DEMO),
        "--param",
        "needle=cited_item",
    )
    assert c_res.exit_code == 0, c_res.output
    [ev_before] = q(ws_root, "SELECT * FROM evidence_review WHERE origin = 'record'")
    assert ev_before["span_text"] == "cited_item"
    cited_text_id = ev_before["extracted_text_id"]

    # Upgrade parser to v2 and re-parse; both v1 rows are now superseded.
    monkeypatch.setitem(PARSERS, "json", orig_json)
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0

    p_res = cli("prune", "--workspace", str(ws_root), "--json")
    assert p_res.exit_code == 0, p_res.output
    report = json.loads(p_res.stdout)
    # Only uncited.json's v1 row is pruned; cited.json's v1 row is protected by evidence.
    assert report["pruned_rows"] == 1

    [cited_row] = q(ws_root, "SELECT * FROM extracted_text WHERE id = ?", cited_text_id)
    assert cited_row["pruned_at"] is None
    assert "cited_item" in cited_row["text"]
    assert cited_row["text"][ev_before["span_start"] : ev_before["span_end"]] == "cited_item"

    [ev_after] = q(ws_root, "SELECT * FROM evidence_review WHERE origin = 'record'")
    assert ev_after["span_text"] == "cited_item"


def test_prune_dry_run_second_run_and_lock(cli, ws_root, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text(json.dumps({"name": "a", "amount": 1}), encoding="utf-8")
    assert cli.ingest(ws_root, src)[0] == 0

    orig_json = PARSERS["json"]
    monkeypatch.setitem(PARSERS, "json", _fake_parser(orig_json, "v1", "extra"))
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    monkeypatch.setitem(PARSERS, "json", orig_json)
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0

    rows_before = q(ws_root, "SELECT * FROM extracted_text ORDER BY id")

    # 1. --dry-run changes no row (both JSON and human output).
    dry = cli("prune", "--workspace", str(ws_root), "--dry-run", "--json")
    assert dry.exit_code == 0, dry.output
    dry_report = json.loads(dry.stdout)
    assert dry_report["dry_run"] is True
    assert dry_report["pruned_rows"] == 1
    assert dry_report["pruned_bytes"] > 0
    assert q(ws_root, "SELECT * FROM extracted_text ORDER BY id") == rows_before
    dry_human = cli("prune", "--workspace", str(ws_root), "--dry-run")
    assert dry_human.exit_code == 0 and "would prune 1 row(s)" in dry_human.stdout

    # 2. Locked workspace exits 3.
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(ws_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        locked = cli("prune", "--workspace", str(ws_root))
        assert locked.exit_code == 3
        assert "locked" in locked.stderr
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)

    # 3. First real prune clears the superseded row; second prune changes no row.
    first = cli("prune", "--workspace", str(ws_root), "--json")
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["pruned_rows"] == 1
    rows_after_first = q(ws_root, "SELECT * FROM extracted_text ORDER BY id")
    assert sum(1 for r in rows_after_first if r["pruned_at"] is not None) == 1

    second = cli("prune", "--workspace", str(ws_root), "--json")
    assert second.exit_code == 0, second.output
    second_report = json.loads(second.stdout)
    assert second_report["pruned_rows"] == 0
    assert second_report["pruned_bytes"] == 0
    assert second_report["by_parser"] == []
    assert q(ws_root, "SELECT * FROM extracted_text ORDER BY id") == rows_after_first


def test_every_reader_reports_pruned_row_as_pruned_never_empty_text(cli, tmp_path):
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    (recipe_dir / "recipe.py").write_text(
        textwrap.dedent(
            """
            from pydantic import BaseModel, ConfigDict, Field
            from commissure.api import Check, Evidence, Interface, Stage

            class Cited(BaseModel):
                model_config = ConfigDict(extra="forbid")
                evidence: list[Evidence] = Field(min_length=1)

            def cite(ctx, item):
                idx = item.text.index("needle_word")
                return Cited(
                    evidence=[Evidence.span(item, idx, idx + len("needle_word"), kind="quote")]
                )

            STAGES = [
                Stage(
                    "cite",
                    cite,
                    schema=Cited,
                    text="default",
                    checks=(Check("/evidence/*", "Does it match?"),),
                )
            ]
            INTERFACE = Interface(title="Test", render=lambda view: "<p>ok</p>")
            """
        ),
        encoding="utf-8",
    )
    ws_root = tmp_path / "workspace"
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.json").write_text(
        json.dumps({"body": "contains needle_word inside"}), encoding="utf-8"
    )

    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    assert (
        cli(
            "run", "--step", "cite", "--workspace", str(ws_root), "--recipe", str(recipe_dir)
        ).exit_code
        == 0
    )
    assert (
        cli(
            "review",
            "sample",
            "--workspace",
            str(ws_root),
            "--recipe",
            str(recipe_dir),
            "--check",
            "cite:/evidence/*",
            "--n",
            "1",
            "--seed",
            "1",
            "--reviewer",
            "alice",
        ).exit_code
        == 0
    )

    # Mark the extracted_text row pruned directly to test every reader against a pruned row.
    ws = open_workspace(ws_root)
    try:
        with transaction(ws.conn):
            ws.conn.execute(
                "UPDATE extracted_text SET text = '', structure = '{}',"
                " pruned_at = '2026-11-28T00:00:00.000Z'"
            )
    finally:
        ws.close()

    # 1. current_text excludes pruned rows, so recon/search/stage readers never see empty text.
    assert q(ws_root, "SELECT * FROM current_text") == []

    # 2. evidence_review reports span_text as NULL, never "".
    [er] = q(ws_root, "SELECT * FROM evidence_review WHERE origin = 'record'")
    assert er["span_text"] is None

    # 3. inspect --subject reports the text row and evidence as pruned in both JSON and terminal.
    insp_json = cli("inspect", "--workspace", str(ws_root), "--subject", "doc.json", "--json")
    assert insp_json.exit_code == 0, insp_json.output
    detail = json.loads(insp_json.stdout)["workspaces"][0]["subject_detail"]
    [t_info] = detail["items"][0]["texts"]
    assert t_info["pruned_at"] == "2026-11-28T00:00:00.000Z"
    assert t_info["text"] is None
    assert t_info["chars"] is None
    [ev_info] = [e for e in detail["evidence"] if e["origin"] == "record"]
    assert ev_info["span_text"] is None
    assert ev_info["context_before"] is None
    assert ev_info["context_after"] is None
    assert ev_info["pruned_at"] == "2026-11-28T00:00:00.000Z"

    insp_human = cli("inspect", "--workspace", str(ws_root), "--subject", "doc.json")
    assert insp_human.exit_code == 0
    assert "pruned" in insp_human.stdout
    assert "0 chars" not in insp_human.stdout
    assert "item as a whole" not in insp_human.stdout

    # 4. review next (CLI) and /review/instance/1 (web UI) report pruned, never empty text.
    rev_human = cli("review", "next", "--workspace", str(ws_root), "--recipe", str(recipe_dir))
    assert rev_human.exit_code == 0
    assert "pruned" in rev_human.stdout

    ui = InterfaceApp(ws_root, recipe_dir)
    try:
        resp = ui._review.get("/review/instance/1", {}, {})
        html = resp.body.decode("utf-8")
        assert "pruned" in html.lower()
        assert "item as a whole" not in html.lower()
    finally:
        ui.shutdown()


def test_prune_vacuum_shrinks_state_db(cli, ws_root, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    big_payload = {"rows": [f"line_{i}_" + ("x" * 200) for i in range(800)]}
    (src / "big.json").write_text(json.dumps(big_payload), encoding="utf-8")
    assert cli.ingest(ws_root, src)[0] == 0

    orig_json = PARSERS["json"]
    for idx in range(5):
        monkeypatch.setitem(PARSERS, "json", _fake_parser(orig_json, f"v{idx}", f"tok_{idx}"))
        assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    monkeypatch.setitem(PARSERS, "json", orig_json)
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0

    # Ensure WAL is checkpointed into state.db so we can compare state.db file size on disk.
    ws = open_workspace(ws_root)
    ws.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ws.close()
    size_before = (ws_root / DB_NAME).stat().st_size

    # Prune without --vacuum first: frees pages and reports reclaimable_bytes > 0.
    no_vac = cli("prune", "--workspace", str(ws_root), "--json")
    assert no_vac.exit_code == 0, no_vac.output
    no_vac_rep = json.loads(no_vac.stdout)
    assert no_vac_rep["pruned_rows"] == 5
    assert no_vac_rep["reclaimable_bytes"] > 100_000
    assert no_vac_rep["vacuum"] is False

    # Now run prune --vacuum: shrinks state.db on disk.
    vac = cli("prune", "--workspace", str(ws_root), "--vacuum", "--json")
    assert vac.exit_code == 0, vac.output
    vac_rep = json.loads(vac.stdout)
    assert vac_rep["vacuum"] is True
    assert vac_rep["reclaimable_bytes"] == 0
    assert vac_rep["reclaimed_bytes"] > 100_000
    size_after = (ws_root / DB_NAME).stat().st_size
    assert size_after < size_before
    assert vac_rep["db_bytes_after"] == size_after
