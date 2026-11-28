"""`commissure run --step ingest` end to end through the CLI."""

import json
import os
import shutil
from pathlib import Path

from commissure.workspace import hash_file, open_workspace, transaction

ALL = ["alpha.txt", "beta.txt", "nested/delta.csv", "nested/gamma.txt"]


def _versions(ws_root, locator):
    ws = open_workspace(ws_root)
    rows = ws.conn.execute(
        "SELECT v.* FROM item_versions v JOIN items i ON i.id = v.item_id"
        " WHERE i.locator = ? ORDER BY v.id",
        (locator,),
    ).fetchall()
    ws.close()
    return [dict(r) for r in rows]


def _query(ws_root, sql, *params):
    ws = open_workspace(ws_root)
    rows = [dict(r) for r in ws.conn.execute(sql, params)]
    ws.close()
    return rows


def test_first_ingest_adds_everything_and_stores_blobs(cli, ws_root, source):
    code, report = cli.ingest(ws_root, source)
    assert code == 0, report
    assert report["status"] == "succeeded"
    assert (report["discovered"], report["added"], report["changed"]) == (4, 4, 0)
    assert (report["unchanged"], report["removed"]) == (0, 0)

    current = _query(ws_root, "SELECT locator, bytes_hash FROM current_items ORDER BY locator")
    assert [r["locator"] for r in current] == ALL
    ws = open_workspace(ws_root)
    for row in current:
        blob = ws.blobs.path_for(row["bytes_hash"])
        assert blob.parent.parent.parent == ws.blobs.root  # two shard levels
        assert blob.read_bytes() == (source / row["locator"]).read_bytes()
        assert hash_file(source / row["locator"]) == row["bytes_hash"]
    ws.close()


def test_second_ingest_classifies_modify_delete_add(cli, ws_root, source):
    cli.ingest(ws_root, source)
    (source / "beta.txt").write_text("beta, edited\n")
    (source / "nested/gamma.txt").unlink()
    (source / "epsilon.txt").write_text("epsilon\n")

    code, report = cli.ingest(ws_root, source)
    assert code == 0, report
    assert report["status"] == "succeeded"
    counts = {k: report[k] for k in ("discovered", "added", "changed", "unchanged", "removed")}
    assert counts == {"discovered": 4, "added": 1, "changed": 1, "unchanged": 2, "removed": 1}

    run = _query(ws_root, "SELECT * FROM runs WHERE id = ?", report["run_id"])[0]
    assert {k: run[k] for k in counts} == counts

    # Unchanged items produced no new version row.
    for locator in ("alpha.txt", "nested/delta.csv"):
        assert len(_versions(ws_root, locator)) == 1

    # The changed item's new version links to its predecessor.
    old, new = _versions(ws_root, "beta.txt")
    assert new["predecessor_id"] == old["id"]
    assert (old["is_current"], new["is_current"]) == (0, 1)
    assert new["run_id"] == report["run_id"]
    assert new["bytes_hash"] != old["bytes_hash"]

    # The removed item is tombstoned, not deleted.
    live, tomb = _versions(ws_root, "nested/gamma.txt")
    assert tomb["tombstone"] == 1 and tomb["is_current"] == 1
    assert tomb["predecessor_id"] == live["id"] and tomb["bytes_hash"] is None

    current = _query(ws_root, "SELECT locator FROM current_items ORDER BY locator")
    assert [r["locator"] for r in current] == [
        "alpha.txt", "beta.txt", "epsilon.txt", "nested/delta.csv",
    ]  # fmt: skip

    drift = _query(
        ws_root,
        "SELECT locator, change, change_kind FROM drift WHERE run_id = ? ORDER BY locator",
        report["run_id"],
    )
    assert drift == [
        {"locator": "beta.txt", "change": "changed", "change_kind": "unknown"},
        {"locator": "epsilon.txt", "change": "added", "change_kind": None},
        {"locator": "nested/gamma.txt", "change": "removed", "change_kind": None},
    ]


def test_unchanged_rerun_is_noop(cli, ws_root, source):
    cli.ingest(ws_root, source)
    code, report = cli.ingest(ws_root, source)
    assert code == 0
    assert report["status"] == "noop"
    assert (report["discovered"], report["unchanged"]) == (4, 4)
    assert (report["added"], report["changed"], report["removed"]) == (0, 0, 0)
    assert _query(ws_root, "SELECT count(*) AS n FROM item_versions")[0]["n"] == 4


def test_readded_locator_is_added_after_its_tombstone(cli, ws_root, source):
    cli.ingest(ws_root, source)
    content = (source / "alpha.txt").read_bytes()
    (source / "alpha.txt").unlink()
    cli.ingest(ws_root, source)
    (source / "alpha.txt").write_bytes(content)
    code, report = cli.ingest(ws_root, source)
    assert code == 0
    assert (report["added"], report["unchanged"]) == (1, 3)
    first, tomb, back = _versions(ws_root, "alpha.txt")
    assert tomb["tombstone"] == 1 and back["predecessor_id"] == tomb["id"]
    assert back["bytes_hash"] == first["bytes_hash"]
    drift = _query(ws_root, "SELECT change FROM drift WHERE run_id = ?", report["run_id"])
    assert drift == [{"change": "added"}]


def test_empty_discovery_fails_closed_without_tombstoning(cli, ws_root, source):
    cli.ingest(ws_root, source)
    shutil.rmtree(source)
    source.mkdir()

    code, report = cli.ingest(ws_root, source)
    assert code == 1
    assert report["status"] == "failed" and "EmptyDiscovery" in report["error"]
    run = _query(ws_root, "SELECT status, discovered FROM runs WHERE id = ?", report["run_id"])
    assert run == [{"status": "failed", "discovered": 0}]
    assert len(_query(ws_root, "SELECT * FROM current_items")) == 4
    assert _query(ws_root, "SELECT count(*) AS n FROM item_versions")[0]["n"] == 4


def test_allow_empty_permits_empty_discovery(cli, ws_root, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code, report = cli.ingest(ws_root, empty, "--allow-empty")
    assert code == 0
    assert (report["status"], report["discovered"]) == ("noop", 0)


def test_unreadable_file_is_quarantined_per_item_while_readable_commit(cli, ws_root, source):
    cli.ingest(ws_root, source)
    (source / "beta.txt").write_text("beta, edited\n")
    new_locked = source / "new_locked.txt"
    new_locked.write_text("locked\n")
    unreadable = source / "alpha.txt"
    unreadable.chmod(0)
    new_locked.chmod(0)
    try:
        code, report = cli.ingest(ws_root, source)
    finally:
        unreadable.chmod(0o644)
        new_locked.chmod(0o644)
    assert code == 0, report
    assert report["status"] == "succeeded"
    assert (report["discovered"], report["added"], report["changed"]) == (5, 0, 1)
    assert (report["unchanged"], report["removed"], report["quarantined"]) == (2, 0, 2)
    # beta.txt committed its new version; alpha.txt kept its prior valid version untouched.
    assert len(_versions(ws_root, "beta.txt")) == 2
    alpha_versions = _versions(ws_root, "alpha.txt")
    assert len(alpha_versions) == 1 and alpha_versions[0]["is_current"] == 1
    assert alpha_versions[0]["tombstone"] == 0

    issues = _query(
        ws_root,
        "SELECT locator, outcome, reason_code FROM outcome_issues"
        " WHERE run_id = ? ORDER BY locator",
        report["run_id"],
    )
    assert issues == [
        {"locator": "alpha.txt", "outcome": "quarantined", "reason_code": "permission_denied"},
        {"locator": "new_locked.txt", "outcome": "quarantined", "reason_code": "permission_denied"},
    ]

    # Once readable again, re-ingesting clears the ingest quarantine from current_outcomes.
    code2, report2 = cli.ingest(ws_root, source)
    assert (code2, report2["added"], report2["quarantined"]) == (0, 1, 0)
    assert _query(ws_root, "SELECT * FROM current_outcomes WHERE stage = 'ingest'") == []


def test_all_unreadable_fails_ingest_and_records_quarantines(cli, ws_root, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    for name in ("a.txt", "b.txt"):
        p = locked / name
        p.write_text("secret\n")
        p.chmod(0)
    try:
        code, report = cli.ingest(ws_root, locked)
    finally:
        for p in locked.iterdir():
            p.chmod(0o644)
    assert (code, report["status"], report["discovered"], report["quarantined"]) == (
        1,
        "failed",
        2,
        2,
    )
    assert "AllQuarantined" in report["error"]
    assert len(_query(ws_root, "SELECT * FROM outcome_issues WHERE stage = 'ingest'")) == 2


def test_infrastructure_fault_during_ingest_fails_run_and_writes_no_versions(cli, ws_root, source):
    cli.ingest(ws_root, source)
    (source / "beta.txt").write_text("beta, edited\n")
    blobs = ws_root / "blobs"
    blobs.chmod(0o555)
    try:
        code, report = cli.ingest(ws_root, source)
    finally:
        blobs.chmod(0o755)
    assert code == 1
    assert report["status"] == "failed" and "PermissionError" in report["error"]
    assert _query(ws_root, "SELECT count(*) AS n FROM item_versions")[0]["n"] == 4


def test_default_ignore_list_and_overrides(cli, ws_root, source):
    (source / ".DS_Store").write_bytes(b"\x00\x01")
    (source / "nested/Thumbs.db").write_bytes(b"\x00\x02")
    (source / "desktop.ini").write_text("[.ShellClassInfo]\n")
    (source / "~$report.docx").write_bytes(b"lock")
    (source / ".~lock.sheet.xlsx#").write_text("lock")
    (source / ".env_keep").write_text("DOTFILE=1\n")  # ordinary dotfiles are still items

    code, report = cli.ingest(ws_root, source)
    assert (code, report["discovered"], report["ignored"]) == (0, 5, 5)
    run_row = _query(ws_root, "SELECT ignored, config FROM runs WHERE id = ?", report["run_id"])[0]
    assert run_row["ignored"] == 5
    cfg = json.loads(run_row["config"])
    assert cfg["effective_ignore"] == [
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        "~$*",
        ".~lock.*#",
    ]

    probed = json.loads(cli("probe", str(source), "--json").stdout)
    assert (probed["summary"]["files"], probed["summary"]["ignored"]) == (5, 5)

    code2, report2 = cli.ingest(ws_root, source, "--ignore", "*.csv")
    assert (code2, report2["discovered"], report2["ignored"], report2["removed"]) == (0, 4, 6, 1)
    probed2 = json.loads(cli("probe", str(source), "--ignore", "*.csv", "--json").stdout)
    assert (probed2["summary"]["files"], probed2["summary"]["ignored"]) == (4, 6)

    code3, report3 = cli.ingest(ws_root, source, "--no-default-ignore")
    assert (code3, report3["discovered"], report3["ignored"]) == (0, 10, 0)
    probed3 = json.loads(cli("probe", str(source), "--no-default-ignore", "--json").stdout)
    assert (probed3["summary"]["files"], probed3["summary"]["ignored"]) == (10, 0)

    assert (
        cli("run", "--step", "parse", "--workspace", str(ws_root), "--ignore", "*.csv").exit_code
        == 2
    )
    assert (
        cli("run", "--step", "parse", "--workspace", str(ws_root), "--no-default-ignore").exit_code
        == 2
    )


def test_config_records_which_source_won(cli, ws_root, source):
    result = cli(
        "run", "--step", "ingest", "--source", str(source), "--json",
        env={"COMMISSURE_WORKSPACE": str(ws_root)},
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    run_id = json.loads(result.stdout)["run_id"]
    run = _query(ws_root, "SELECT trigger, config FROM runs WHERE id = ?", run_id)[0]
    config = json.loads(run["config"])
    assert config["workspace"] == {"value": str(ws_root), "from": "environment"}
    assert config["source"]["from"] == "commandline"
    assert config["role"] == {"value": "corpus", "from": "default"}
    assert run["trigger"] == "manual"


def test_latest_runs_view_shows_most_recent_per_step_and_source(cli, ws_root, source):
    cli.ingest(ws_root, source)
    _, second = cli.ingest(ws_root, source)
    rows = _query(ws_root, "SELECT id, step, status FROM latest_runs")
    assert rows == [{"id": second["run_id"], "step": "ingest", "status": "noop"}]


def test_usage_errors_exit_2_and_record_no_run(cli, ws_root, source, tmp_path):
    assert cli.ingest(ws_root, source, "--limit", "2")[0] == 2
    assert cli.ingest(ws_root, tmp_path / "missing")[0] == 2
    inside = source / "ws"
    assert cli.ingest(inside, source)[0] == 2
    assert not inside.exists()

    cli.ingest(ws_root, source)
    assert cli.ingest(ws_root, source, "--role", "reference")[0] == 2
    assert len(_query(ws_root, "SELECT * FROM runs")) == 1


def test_source_files_are_not_modified(cli, ws_root, source):
    before = {p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in source.rglob("*") if p.is_file()}
    cli.ingest(ws_root, source)
    after = {p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in source.rglob("*") if p.is_file()}
    assert before == after


def test_drift_compares_text_only_under_the_same_parser(cli, ws_root, source):
    cli.ingest(ws_root, source)
    for name in ("alpha.txt", "beta.txt", "nested/delta.csv"):
        (source / name).write_bytes((source / name).read_bytes() + b"\n")
    _, report = cli.ingest(ws_root, source)
    run_id = report["run_id"]

    ws = open_workspace(ws_root)

    def text(locator, which, parser, version, text_hash):
        old, new = _versions(ws_root, locator)
        ws.conn.execute(
            "INSERT INTO extracted_text (item_version_id, parser, parser_version, text,"
            " text_hash, run_id, created_at) VALUES (?, ?, ?, 'x', ?, ?, '')",
            ({"old": old, "new": new}[which]["id"], parser, version, text_hash, run_id),
        )

    # Same parser, same text: the byte change is metadata only.
    text("alpha.txt", "old", "p", "1", "t1")
    text("alpha.txt", "new", "p", "1", "t1")
    # Same parser, different text: an upstream content change.
    text("beta.txt", "old", "p", "1", "t1")
    text("beta.txt", "new", "p", "1", "t2")
    # Different parser versions only: not comparable, never called a source edit.
    text("nested/delta.csv", "old", "p", "1", "t1")
    text("nested/delta.csv", "new", "p", "2", "t2")
    ws.close()

    drift = _query(
        ws_root,
        "SELECT locator, change_kind FROM drift WHERE run_id = ? ORDER BY locator",
        run_id,
    )
    assert drift == [
        {"locator": "alpha.txt", "change_kind": "metadata_only"},
        {"locator": "beta.txt", "change_kind": "content"},
        {"locator": "nested/delta.csv", "change_kind": "unknown"},
    ]


def test_symlinks_are_not_items(cli, ws_root, source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.txt").write_text("not in the source\n")
    (source / "link.txt").symlink_to(source / "alpha.txt")
    (source / "linked_dir").symlink_to(outside, target_is_directory=True)

    _, report = cli.ingest(ws_root, source)
    assert report["discovered"] == 4
    probed = json.loads(cli("probe", str(source), "--json").stdout)
    assert probed["summary"]["files"] == 4


def test_folder_source_inside_solution_stored_relative_and_survives_move(cli, tmp_path):
    sol_a = tmp_path / "a" / "demo"
    assert cli("init", str(sol_a)).exit_code == 0
    batch_a = sol_a / "intake" / "batch"
    batch_a.mkdir(parents=True)
    (batch_a / "one.txt").write_text("hello\n")

    code, first = cli.ingest(sol_a / "workspace", batch_a)
    assert code == 0, first
    assert (first["added"], first["unchanged"]) == (1, 0)
    assert _query(sol_a / "workspace", "SELECT id, uri FROM sources") == [
        {"id": 1, "uri": "intake/batch"}
    ]

    sol_b = tmp_path / "b" / "demo"
    shutil.move(tmp_path / "a", tmp_path / "b")
    code2, second = cli.ingest(sol_b / "workspace", sol_b / "intake" / "batch")
    assert code2 == 0, second
    assert (second["status"], second["added"], second["unchanged"]) == ("noop", 0, 1)
    assert _query(sol_b / "workspace", "SELECT id, uri FROM sources") == [
        {"id": 1, "uri": "intake/batch"}
    ]
    assert _query(sol_b / "workspace", "SELECT count(*) AS n FROM items")[0]["n"] == 1

    # A source outside the solution folder is still stored as an absolute path.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "two.txt").write_text("world\n")
    code3, third = cli.ingest(sol_b / "workspace", outside)
    assert code3 == 0, third
    assert _query(sol_b / "workspace", "SELECT id, uri FROM sources ORDER BY id") == [
        {"id": 1, "uri": "intake/batch"},
        {"id": 2, "uri": str(outside.resolve())},
    ]


def test_readers_resolve_both_stored_source_forms_to_absolute_paths(cli, tmp_path):
    sol = tmp_path / "sol"
    ws_root = sol / "workspace"
    inside = sol / "intake" / "batch"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir(parents=True)
    (inside / "in.html").write_text("<main>inside text</main>\n")
    (outside / "out.html").write_text("<main>outside text</main>\n")

    recipe_dir = sol / "recipe"
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "recipe.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Check, Evidence, Interface, Stage\n"
        "class Out(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    source_uri: str\n"
        "    ev: Evidence\n"
        "def run(ctx, item):\n"
        "    return Out(source_uri=item.source.uri, ev=Evidence.span(item, 0, 6, kind='lead'))\n"
        "STAGES = [\n"
        "    Stage('check_uri', run, schema=Out, text='default',\n"
        "          checks=(Check('/source_uri', 'Is source_uri absolute?'),))\n"
        "]\n"
        "INTERFACE = Interface(title='Demo', render=lambda v: '<p>ok</p>')\n"
    )

    assert cli.ingest(ws_root, inside)[0] == 0
    assert cli.ingest(ws_root, outside)[0] == 0
    assert _query(ws_root, "SELECT uri FROM sources ORDER BY id") == [
        {"uri": "intake/batch"},
        {"uri": str(outside.resolve())},
    ]
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    assert (
        cli(
            "run", "--step", "check_uri", "--workspace", str(ws_root), "--recipe", str(recipe_dir)
        ).exit_code
        == 0
    )

    inside_abs = str(inside.resolve())
    outside_abs = str(outside.resolve())

    # 1. Recipes receive resolved absolute source.uri for both stored forms.
    for filename, expected_dir in (("in.html", inside_abs), ("out.html", outside_abs)):
        insp = json.loads(
            cli("inspect", "--workspace", str(ws_root), "--subject", filename, "--json").stdout
        )["workspaces"][0]["subject_detail"]
        assert insp["records"][0]["payload"]["source_uri"] == expected_dir
        expected_file = str((Path(expected_dir) / filename).resolve())
        assert insp["items"][0]["original_path"] == expected_file
        assert insp["items"][0]["original_status"] == "present"
        assert insp["evidence"][0]["original_path"] == expected_file
        assert insp["evidence"][0]["original_status"] == "present"

    # 2. Review (and the review page via review.describe) sees resolved absolute original_path.
    assert (
        cli(
            "review",
            "sample",
            "--workspace",
            str(ws_root),
            "--recipe",
            str(recipe_dir),
            "--check",
            "check_uri:/source_uri",
            "--n",
            "2",
            "--seed",
            "1",
            "--reviewer",
            "tester",
        ).exit_code
        == 0
    )
    for inst_id in (1, 2):
        inst = json.loads(
            cli(
                "review",
                "next",
                "--workspace",
                str(ws_root),
                "--recipe",
                str(recipe_dir),
                "--instance",
                str(inst_id),
                "--json",
            ).stdout
        )["instance"]
        orig_path = inst["originals"][0]["original_path"]
        assert Path(orig_path).is_absolute()
        assert inst["originals"][0]["original_status"] == "present"

    # 3. Serve InterfaceView and review_page see resolved absolute URIs.
    from commissure.recipes import load_recipe
    from commissure.serve import InterfaceApp, build_view

    view = build_view(ws_root, load_recipe(recipe_dir), "token")
    assert [s.uri for s in view.sources] == [inside_abs, outside_abs]
    ui = InterfaceApp(ws_root, recipe_dir)
    resp = ui._review.get("/review/instance/1", {}, {})
    assert resp.status == 200
    assert "/review/original/" in resp.body.decode("utf-8")


def test_sources_relocate_and_ingest_stale_source_warning(cli, tmp_path):

    old_sol = tmp_path / "old" / "demo"
    old_ws = old_sol / "workspace"
    old_batch = old_sol / "intake" / "batch"
    old_ext = tmp_path / "ext_old" / "corpus"
    old_batch.mkdir(parents=True)
    old_ext.mkdir(parents=True)
    (old_batch / "one.html").write_text("<main>alpha beta</main>\n")
    (old_ext / "two.html").write_text("<main>gamma delta</main>\n")

    recipe_dir = old_sol / "recipe"
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "recipe.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Check, Evidence, Stage\n"
        "class Out(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    word: str\n"
        "    ev: Evidence\n"
        "def extract(ctx, item):\n"
        "    return Out(word=item.text.split()[0], ev=Evidence.span(item, 0, 5, kind='word'))\n"
        "STAGES = [\n"
        "    Stage('extract', extract, schema=Out, text='default',\n"
        "          checks=(Check('/word', 'Is the first word right?'),))\n"
        "]\n"
    )

    assert cli.ingest(old_ws, old_batch)[0] == 0
    assert cli.ingest(old_ws, old_ext)[0] == 0
    # Simulate a pre-change workspace where intake/batch was stored as an absolute path.
    ws = open_workspace(old_ws)
    with transaction(ws.conn):
        ws.conn.execute("UPDATE sources SET uri = ? WHERE id = 1", (str(old_batch.resolve()),))
    ws.close()

    assert cli("run", "--step", "parse", "--workspace", str(old_ws)).exit_code == 0
    assert (
        cli(
            "run", "--step", "extract", "--workspace", str(old_ws), "--recipe", str(recipe_dir)
        ).exit_code
        == 0
    )
    assert (
        cli(
            "review",
            "sample",
            "--workspace",
            str(old_ws),
            "--recipe",
            str(recipe_dir),
            "--check",
            "extract:/word",
            "--n",
            "2",
            "--seed",
            "7",
            "--reviewer",
            "alice",
        ).exit_code
        == 0
    )
    assert (
        cli(
            "review", "record", "1", "correct", "--workspace", str(old_ws), "--reviewer", "alice"
        ).exit_code
        == 0
    )

    def snapshot_counts(root: Path) -> dict[str, int]:
        return {
            table: _query(root, f"SELECT count(*) AS n FROM {table}")[0]["n"]
            for table in (
                "sources",
                "items",
                "item_versions",
                "outcomes",
                "evidence",
                "review_instances",
                "verdicts",
            )
        }

    before_counts = snapshot_counts(old_ws)

    # Move both the solution folder and the external folder.
    new_sol = tmp_path / "new" / "demo"
    new_ws = new_sol / "workspace"
    new_recipe = new_sol / "recipe"
    new_ext = tmp_path / "ext_new" / "corpus"
    shutil.move(tmp_path / "old", tmp_path / "new")
    shutil.move(tmp_path / "ext_old", tmp_path / "ext_new")

    # Refusal 1: <new> does not exist.
    r_missing = cli(
        "sources", "relocate", str(old_sol), str(tmp_path / "missing"), "--workspace", str(new_ws)
    )
    assert r_missing.exit_code == 2
    assert "does not exist" in r_missing.output
    assert _query(new_ws, "SELECT id, uri FROM sources ORDER BY id") == [
        {"id": 1, "uri": str(old_batch.resolve())},
        {"id": 2, "uri": str(old_ext.resolve())},
    ]

    # Refusal 2: <new> exists, but the rewritten subfolder under it does not exist.
    empty_dir = tmp_path / "empty_target"
    empty_dir.mkdir()
    r_sub_missing = cli(
        "sources", "relocate", str(old_sol), str(empty_dir), "--workspace", str(new_ws)
    )
    assert r_sub_missing.exit_code == 2
    assert "intake/batch" in r_sub_missing.output and "does not exist" in r_sub_missing.output
    assert _query(new_ws, "SELECT id, uri FROM sources ORDER BY id") == [
        {"id": 1, "uri": str(old_batch.resolve())},
        {"id": 2, "uri": str(old_ext.resolve())},
    ]

    # --dry-run writes nothing.
    r_dry = cli(
        "sources",
        "relocate",
        str(old_sol),
        str(new_sol),
        "--workspace",
        str(new_ws),
        "--dry-run",
        "--json",
    )
    assert r_dry.exit_code == 0, r_dry.output
    dry_report = json.loads(r_dry.stdout)
    assert dry_report["dry_run"] is True
    assert dry_report["relocated"] == [
        {"id": 1, "old_uri": str(old_batch.resolve()), "new_uri": "intake/batch"}
    ]
    assert _query(new_ws, "SELECT id, uri FROM sources ORDER BY id") == [
        {"id": 1, "uri": str(old_batch.resolve())},
        {"id": 2, "uri": str(old_ext.resolve())},
    ]

    # Relocate the solution folder (stored relative) and the external source (stored absolute).
    r_sol = cli(
        "sources", "relocate", str(old_sol), str(new_sol), "--workspace", str(new_ws), "--json"
    )
    assert r_sol.exit_code == 0, r_sol.output
    r_ext = cli(
        "sources",
        "relocate",
        str(tmp_path / "ext_old"),
        str(tmp_path / "ext_new"),
        "--workspace",
        str(new_ws),
        "--json",
    )
    assert r_ext.exit_code == 0, r_ext.output
    assert _query(new_ws, "SELECT id, uri FROM sources ORDER BY id") == [
        {"id": 1, "uri": "intake/batch"},
        {"id": 2, "uri": str(new_ext.resolve())},
    ]
    assert snapshot_counts(new_ws) == before_counts

    # Every stage is fresh after relocation (relocation is not an input change).
    assert cli.ingest(new_ws, new_sol / "intake" / "batch")[1]["status"] == "noop"
    assert cli.ingest(new_ws, new_ext)[1]["status"] == "noop"
    p_res = cli("run", "--step", "parse", "--workspace", str(new_ws), "--json")
    assert p_res.exit_code == 0 and json.loads(p_res.stdout)["status"] == "noop"
    insp = json.loads(cli("inspect", "--workspace", str(new_ws), "--json").stdout)["workspaces"][0]
    for row in insp["outcomes"]:
        assert (
            row["version_stale"],
            row["code_stale"],
            row["params_stale"],
            row["input_stale"],
        ) == (0, 0, 0, 0)
    checks_out = json.loads(
        cli(
            "review",
            "checks",
            "--workspace",
            str(new_ws),
            "--recipe",
            str(new_recipe),
            "--json",
        ).stdout
    )["checks"]
    extract_check = next(c for c in checks_out if c["id"] == "extract:/word")
    assert extract_check["population"] == 2 and extract_check.get("unavailable") is None

    # Now test stale-source warning on ingest and collision refusal on relocate:
    # Move new_ext to ext_v3, and ingest ext_v3 BEFORE running relocate.
    ext_v3 = tmp_path / "ext_v3" / "corpus"
    shutil.move(tmp_path / "ext_new", tmp_path / "ext_v3")
    ing_dup = cli(
        "run", "--step", "ingest", "--workspace", str(new_ws), "--source", str(ext_v3), "--json"
    )
    assert ing_dup.exit_code == 0, ing_dup.output
    # Should have warned on stderr naming `commissure sources relocate`.
    assert "commissure sources relocate" in ing_dup.output

    # Now trying to relocate ext_new -> ext_v3 collides with source #3; refuses and names both
    # sources with their item, outcome, and verdict-instance counts.
    r_collide = cli(
        "sources",
        "relocate",
        str(tmp_path / "ext_new"),
        str(tmp_path / "ext_v3"),
        "--workspace",
        str(new_ws),
    )
    assert r_collide.exit_code != 0
    assert str(new_ext.resolve()) in r_collide.output
    assert str(ext_v3.resolve()) in r_collide.output
    assert "1 item" in r_collide.output
    assert "outcome" in r_collide.output
    assert "verdict" in r_collide.output
    # Nothing was written by the refused relocate.
    assert _query(new_ws, "SELECT id, uri FROM sources ORDER BY id") == [
        {"id": 1, "uri": "intake/batch"},
        {"id": 2, "uri": str(new_ext.resolve())},
        {"id": 3, "uri": str(ext_v3.resolve())},
    ]


def test_unmoved_absolute_source_from_older_workspace_is_adopted_not_duplicated(cli, tmp_path):
    # A workspace written before relative storage holds the same, still-valid folder as an
    # absolute path. The next ingest must reuse that source (rewritten to the relative form),
    # not register a second one and re-add every item.
    sol = tmp_path / "demo"
    assert cli("init", str(sol)).exit_code == 0
    batch = sol / "intake" / "batch"
    batch.mkdir(parents=True)
    (batch / "one.txt").write_text("hello\n")
    code, first = cli.ingest(sol / "workspace", batch)
    assert code == 0, first
    ws = open_workspace(sol / "workspace")
    with transaction(ws.conn):
        ws.conn.execute("UPDATE sources SET uri = ? WHERE id = 1", (str(batch.resolve()),))
    ws.close()

    code2, second = cli.ingest(sol / "workspace", batch)
    assert code2 == 0, second
    assert (second["status"], second["added"], second["unchanged"]) == ("noop", 0, 1)
    assert _query(sol / "workspace", "SELECT id, uri FROM sources") == [
        {"id": 1, "uri": "intake/batch"}
    ]
    assert _query(sol / "workspace", "SELECT count(*) AS n FROM items")[0]["n"] == 1
