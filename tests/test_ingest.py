"""`commissure run --step ingest` end to end through the CLI."""

import json
import os
import shutil

from commissure.workspace import hash_file, open_workspace

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
