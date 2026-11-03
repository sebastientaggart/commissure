"""`commissure inspect` reads a workspace and writes nothing."""

import json
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from commissure.inspect import _cell
from commissure.workspace import open_workspace

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


def test_unmeasured_run_signals_stay_null(cli, ws_root, source):
    code, _ = cli.ingest(ws_root, source)
    assert code == 0
    result = cli("inspect", "--workspace", str(ws_root), "--json")
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report["workspaces"]) == 1
    ws = report["workspaces"][0]
    assert Path(ws["workspace"]) == ws_root.resolve()
    assert ws["counts"] == {
        "sources": 1,
        "current_items": 4,
        "item_versions": 4,
        "tombstones": 0,
        "entities": 0,
        "edges": 0,
        "by_role": {"corpus": 4},
    }
    assert ws["drift"] == [{"change": "added", "change_kind": None, "n": 4}]
    run = ws["runs"][0]
    assert run["step"] == "ingest"
    assert run["status"] == "succeeded"
    assert run["discovered"] == 4
    assert run["quarantined"] == 0
    # Ingest does not measure these. A report that fills them with zero is wrong.
    assert run["attempted"] is None
    assert run["valid"] is None
    assert run["warning"] is None
    assert run["skipped"] is None
    assert ws["latest_runs"][0]["id"] == run["id"]
    assert ws["outcomes"] == []
    assert ws["issues"] == []

    human = cli("inspect", "--workspace", str(ws_root))
    assert human.exit_code == 0, human.stderr
    assert "4 current items" in human.stdout
    assert _cell(None) == "-"


def test_cell_renders_null_as_a_dash():
    assert _cell(None) == "-"
    assert _cell(0) == "0"


def test_issues_and_outcomes_come_from_the_views(cli, ws_root, source):
    assert cli.ingest(ws_root, source)[0] == 0
    parsed = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json")
    assert parsed.exit_code == 0, parsed.stdout
    result = cli("inspect", "--workspace", str(ws_root), "--json")
    assert result.exit_code == 0, result.stderr
    ws = json.loads(result.stdout)["workspaces"][0]
    skipped = [row for row in ws["outcomes"] if row["outcome"] == "skipped"]
    assert skipped == [
        {
            "stage": "parse",
            "outcome": "skipped",
            "n": 3,
            "version_stale": 0,
            "code_stale": 0,
            "params_stale": 0,
            "input_stale": 0,
        }
    ]
    assert sum(row["n"] for row in ws["outcomes"] if row["outcome"] == "valid") == 1
    assert len(ws["issues"]) == 3
    assert {row["reason_code"] for row in ws["issues"]} == {"no_parser"}
    assert all(row["locator"].endswith(".txt") for row in ws["issues"])
    assert isinstance(ws["issues"][0]["diagnostics"], list)

    # Record an LLM call with unknown cost_usd and NULL tokens to verify inspect surfaces
    # llm_calls_by_run, llm_calls_by_model, and llm_calls while keeping NULL sums NULL.
    opened = open_workspace(ws_root)
    try:
        run_id = ws["runs"][0]["id"]
        opened.conn.execute(
            "INSERT INTO llm_calls (run_id, stage, subject_key, provider, model, cache_key,"
            " request_hash, prompt_hash, input_tokens, output_tokens, cost_usd, latency_ms,"
            " cached, repairs_count, status, error, created_at)"
            " VALUES (?, 'demo.extract', 'item:1', 'ollama', 'qwen2.5:7b', 'ck1', 'rh1', 'ph1',"
            " NULL, NULL, NULL, 15, 0, 2, 'transport_error', 'timeout', '2026-10-26T00:00:00Z')",
            (run_id,),
        )
    finally:
        opened.close()

    with_llm = json.loads(cli("inspect", "--workspace", str(ws_root), "--json").stdout)[
        "workspaces"
    ][0]
    assert len(with_llm["llm_calls_by_run"]) == 1
    assert with_llm["llm_calls_by_run"][0]["input_tokens"] is None
    assert with_llm["llm_calls_by_run"][0]["output_tokens"] is None
    assert with_llm["llm_calls_by_run"][0]["cost_usd"] is None
    assert len(with_llm["llm_calls_by_model"]) == 1
    assert with_llm["llm_calls_by_model"][0]["provider"] == "ollama"
    assert with_llm["llm_calls_by_model"][0]["cost_usd"] is None
    assert len(with_llm["llm_calls"]) == 1
    call_row = with_llm["llm_calls"][0]
    for key in (
        "provider",
        "model",
        "subject_key",
        "status",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "latency_ms",
        "repairs_count",
        "cached",
    ):
        assert key in call_row
    human_llm = cli("inspect", "--workspace", str(ws_root))
    assert human_llm.exit_code == 0
    assert "LLM calls" in human_llm.stdout


def test_quarantined_ingest_is_reported_and_inspect_still_succeeds(cli, ws_root, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    path = locked / "a.txt"
    path.write_text("secret\n")
    path.chmod(0)
    try:
        code, _ = cli.ingest(ws_root, locked)
    finally:
        path.chmod(0o644)
    assert code == 1
    result = cli("inspect", "--workspace", str(ws_root), "--json")
    assert result.exit_code == 0, result.stderr
    ws = json.loads(result.stdout)["workspaces"][0]
    assert ws["runs"][0]["status"] == "failed"
    assert ws["runs"][0]["quarantined"] == 1
    assert ws["runs"][0]["attempted"] is None
    assert ws["issues"] == [
        {
            "run_id": ws["runs"][0]["id"],
            "stage": "ingest",
            "subject_kind": "item",
            "subject_key": "item:1",
            "source_id": 1,
            "locator": "a.txt",
            "item_version_id": None,
            "is_current": None,
            "outcome": "quarantined",
            "reason_code": "permission_denied",
            "diagnostics": ws["issues"][0]["diagnostics"],
        }
    ]
    assert ws["issues"][0]["diagnostics"][0]["code"] == "permission_denied"


def test_repeatable_workspace_keeps_command_order(cli, tmp_path, source):
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert cli.ingest(first, source)[0] == 0
    open_workspace(second).close()
    result = cli(
        "inspect",
        "--workspace",
        str(first),
        "--workspace",
        str(second),
        "--json",
    )
    assert result.exit_code == 0, result.stderr
    workspaces = json.loads(result.stdout)["workspaces"]
    assert [Path(item["workspace"]) for item in workspaces] == [first.resolve(), second.resolve()]
    assert workspaces[0]["counts"]["current_items"] == 4
    assert workspaces[1]["counts"]["current_items"] == 0
    assert workspaces[1]["runs"] == []


def test_missing_workspace_is_not_created(cli, tmp_path):
    missing = tmp_path / "nope"
    result = cli("inspect", "--workspace", str(missing))
    assert result.exit_code == 2
    assert "not a directory" in result.stderr
    assert not missing.exists()


def test_inspect_refuses_to_migrate_an_older_database(cli, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    db = root / "state.db"
    sqlite3.connect(db).close()
    before = db.read_bytes()
    result = cli("inspect", "--workspace", str(root))
    assert result.exit_code == 2
    assert "schema version 0" in result.stderr
    assert "does not migrate" in result.stderr
    assert db.read_bytes() == before
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'runs'").fetchone() is None
    finally:
        conn.close()


def test_inspect_does_not_take_the_workspace_lock(cli, ws_root, source):
    assert cli.ingest(ws_root, source)[0] == 0
    db = ws_root / "state.db"
    before_db = db.read_bytes()
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(ws_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        result = cli("inspect", "--workspace", str(ws_root), "--json")
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["workspaces"][0]["counts"]["current_items"] == 4
        # The lock is still held: a writer is refused, and the database is unchanged.
        refused = cli(
            "run",
            "--step",
            "ingest",
            "--workspace",
            str(ws_root),
            "--source",
            str(source),
        )
        assert refused.exit_code == 3
        assert db.read_bytes() == before_db
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    ws = open_workspace(ws_root)
    try:
        assert ws.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    finally:
        ws.close()


def test_solution_dir_and_env_select_the_workspace(cli, tmp_path, source, monkeypatch):
    monkeypatch.delenv("COMMISSURE_WORKSPACE", raising=False)
    sol = tmp_path / "solution"
    (sol / "recipe").mkdir(parents=True)
    assert cli.ingest(sol / "workspace", source)[0] == 0
    monkeypatch.chdir(sol)
    from_dir = cli("inspect", "--json")
    assert from_dir.exit_code == 0, from_dir.stderr
    assert (
        Path(json.loads(from_dir.stdout)["workspaces"][0]["workspace"])
        == (sol / "workspace").resolve()
    )

    other = tmp_path / "other"
    open_workspace(other).close()
    from_env = cli("inspect", "--json", env={"COMMISSURE_WORKSPACE": str(other)})
    assert from_env.exit_code == 0, from_env.stderr
    assert Path(json.loads(from_env.stdout)["workspaces"][0]["workspace"]) == other.resolve()
    # A named workspace wins over the environment and the solution directory.
    named = cli(
        "inspect",
        "--workspace",
        str(sol / "workspace"),
        "--json",
        env={"COMMISSURE_WORKSPACE": str(other)},
    )
    assert (
        Path(json.loads(named.stdout)["workspaces"][0]["workspace"])
        == (sol / "workspace").resolve()
    )


def test_inspect_filters_run_stage_subject_call_only_errors_limit_and_error_columns(
    cli, ws_root, tmp_path
):
    src = tmp_path / "src"
    src.mkdir()
    (src / "good.json").write_text('{"name": "good", "amount": 10}', encoding="utf-8")
    (src / "bad.json").write_text('{"name": "bad", "amount": -5}', encoding="utf-8")
    (src / "note.txt").write_text("plain text skipped by default parse", encoding="utf-8")

    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root), "--json").exit_code == 0

    demo = Path(__file__).parent / "recipes" / "demo"
    r_ext = cli(
        "run",
        "--step",
        "extract",
        "--workspace",
        str(ws_root),
        "--recipe",
        str(demo),
        "--json",
    )
    assert r_ext.exit_code == 0, r_ext.output
    ext_run_id = json.loads(r_ext.stdout)["run_id"]

    # Seed one cached invalid LLM call and one uncached transport_error LLM call + a failed run.
    opened = open_workspace(ws_root)
    try:
        bad_item_key = opened.conn.execute(
            "SELECT 'item:' || id FROM items WHERE locator = 'bad.json'"
        ).fetchone()[0]
        good_item_key = opened.conn.execute(
            "SELECT 'item:' || id FROM items WHERE locator = 'good.json'"
        ).fetchone()[0]
        opened.conn.execute(
            "INSERT INTO llm_cache (cache_key, provider, model, request_json, raw_response,"
            " input_tokens, output_tokens, cost_usd, created_at)"
            " VALUES ('ck_ok', 'llamacpp', 'gpt-oss', '{\"mode\":\"extract\",\"prompt\":\"hi\"}',"
            " '{\"stance\":\"support\"}', 100, 20, NULL, '2026-11-01T00:00:00Z')"
        )
        ok_call_id = opened.conn.execute(
            "INSERT INTO llm_calls (run_id, stage, subject_key, provider, model, cache_key,"
            " request_hash, prompt_hash, input_tokens, output_tokens, cost_usd, latency_ms,"
            " cached, repairs_count, status, error, created_at)"
            " VALUES (?, 'demo.extract', ?, 'llamacpp', 'gpt-oss', 'ck_ok', 'rh1', 'ph1',"
            " 100, 20, NULL, 50, 1, 0, 'ok', NULL, '2026-11-01T00:00:01Z')",
            (ext_run_id, good_item_key),
        ).lastrowid

        failed_run_id = opened.conn.execute(
            "INSERT INTO runs (step, trigger, status, started_at, finished_at, config, error,"
            " discovered, unchanged, attempted, valid, warning, quarantined, skipped, remaining)"
            " VALUES ('demo.stance', 'manual', 'failed', '2026-11-01T01:00:00Z',"
            " '2026-11-01T01:05:00Z', '{}', 'LLMTransportError: Request timed out.',"
            " 2, 0, 1, 1, 0, 0, 0, 1)"
        ).lastrowid
        err_call_id = opened.conn.execute(
            "INSERT INTO llm_calls (run_id, stage, subject_key, provider, model, cache_key,"
            " request_hash, prompt_hash, input_tokens, output_tokens, cost_usd, latency_ms,"
            " cached, repairs_count, status, error, created_at)"
            " VALUES (?, 'demo.stance', ?, 'llamacpp', 'gpt-oss', 'ck_missing', 'rh2', 'ph2',"
            " 4800, NULL, NULL, 300000, 0, 0, 'transport_error',"
            " 'ModelTimeoutError: Request timed out.', '2026-11-01T01:05:00Z')",
            (failed_run_id, bad_item_key),
        ).lastrowid
    finally:
        opened.close()

    # 1. Invalid --run selector fails with usage error (exit 2).
    bad_sel = cli("inspect", "--workspace", str(ws_root), "--run", "not-a-run")
    assert bad_sel.exit_code == 2

    # 2. --run failed + --only-errors surfaces only the failed run and non-ok LLM call.
    failed_only = cli(
        "inspect", "--workspace", str(ws_root), "--run", "failed", "--only-errors", "--json"
    )
    assert failed_only.exit_code == 0, failed_only.stderr
    ws_failed = json.loads(failed_only.stdout)["workspaces"][0]
    assert [r["id"] for r in ws_failed["runs"]] == [failed_run_id]
    assert [c["id"] for c in ws_failed["llm_calls"]] == [err_call_id]

    # 3. --stage extract (short name) matches 'demo.extract' runs, outcomes, issues, and llm_calls.
    by_stage = cli("inspect", "--workspace", str(ws_root), "--stage", "extract", "--json")
    assert by_stage.exit_code == 0, by_stage.stderr
    ws_stage = json.loads(by_stage.stdout)["workspaces"][0]
    assert [r["id"] for r in ws_stage["runs"]] == [ext_run_id]
    assert all(r["stage"] == "demo.extract" for r in ws_stage["outcomes"])
    assert all(r["stage"] == "demo.extract" for r in ws_stage["issues"])
    assert [c["id"] for c in ws_stage["llm_calls"]] == [ok_call_id]

    # 4. --subject bad.json (by locator or subject_key) returns subject_outcomes, issues, llm_calls.
    by_subj = cli("inspect", "--workspace", str(ws_root), "--subject", "bad.json", "--json")
    assert by_subj.exit_code == 0, by_subj.stderr
    ws_subj = json.loads(by_subj.stdout)["workspaces"][0]
    assert len(ws_subj["subject_outcomes"]) >= 2  # parse + demo.extract
    assert all(r["subject_key"] == bad_item_key for r in ws_subj["subject_outcomes"])
    assert [c["id"] for c in ws_subj["llm_calls"]] == [err_call_id]

    # 5. --call <id> joins llm_cache request + raw_response (and reports when absent).
    by_call_ok = cli("inspect", "--workspace", str(ws_root), "--call", str(ok_call_id), "--json")
    assert by_call_ok.exit_code == 0, by_call_ok.stderr
    detail_ok = json.loads(by_call_ok.stdout)["workspaces"][0]["call_detail"]
    assert detail_ok["id"] == ok_call_id
    assert detail_ok["cached_entry_present"] is True
    assert detail_ok["request"]["prompt"] == "hi"
    assert detail_ok["raw_response"] == '{"stance":"support"}'

    by_call_err = cli("inspect", "--workspace", str(ws_root), "--call", str(err_call_id), "--json")
    assert by_call_err.exit_code == 0, by_call_err.stderr
    detail_err = json.loads(by_call_err.stdout)["workspaces"][0]["call_detail"]
    assert detail_err["id"] == err_call_id
    assert detail_err["cached_entry_present"] is False
    assert detail_err["request"] is None
    assert detail_err["raw_response"] is None

    # 6. --limit 1 caps row-level lists to the newest 1 row while keeping summary counts intact.
    limited = cli("inspect", "--workspace", str(ws_root), "--limit", "1", "--json")
    assert limited.exit_code == 0, limited.stderr
    ws_lim = json.loads(limited.stdout)["workspaces"][0]
    assert len(ws_lim["runs"]) == 1 and ws_lim["runs"][0]["id"] == failed_run_id
    assert len(ws_lim["llm_calls"]) == 1 and ws_lim["llm_calls"][0]["id"] == err_call_id
    assert ws_lim["counts"]["current_items"] == 3

    # 7. Human table output includes error details for failed runs and transport_error LLM calls.
    human = cli("inspect", "--workspace", str(ws_root), "--only-errors")
    assert human.exit_code == 0, human.stderr
    assert "Request timed out" in human.stdout


PROBE = Path(__file__).parent / "fixtures" / "probe_corpus"
DEMO = Path(__file__).parent / "recipes" / "demo"


def _subject(cli, ws_root, subject):
    res = cli("inspect", "--workspace", str(ws_root), "--subject", subject, "--json")
    assert res.exit_code == 0, res.stderr
    return json.loads(res.stdout)["workspaces"][0]


def test_subject_detail_links_pages_of_the_original_and_only_while_it_is_unchanged(
    cli, ws_root, tmp_path
):
    """Page links come from parse diagnostics and point at the original, never a changed one.

    scanned.pdf is quarantined (every page image-only, no text row); mixed.pdf is a warning
    with unreadable pages 2, 3, 6 and extracted text. Both are generated probe fixtures.
    """
    src = tmp_path / "src"
    src.mkdir()
    for name in ("scanned.pdf", "mixed.pdf"):
        shutil.copy(PROBE / name, src / name)
    other = tmp_path / "other"
    other.mkdir()
    (other / "unrelated.json").write_text('{"name": "x", "amount": 1}', encoding="utf-8")
    assert cli.ingest(ws_root, src)[0] == 0
    unrelated_ingest = cli.ingest(ws_root, other)[1]["run_id"]
    parse = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json")
    assert parse.exit_code == 0, parse.output

    ws = _subject(cli, ws_root, "scanned.pdf")
    [item] = ws["subject_detail"]["items"]
    assert item["original_path"] == str(src / "scanned.pdf")
    assert item["original_status"] == "present"
    assert item["blob_present"] and Path(item["blob_path"]).is_file()
    assert item["texts"] == []  # quarantined: no text row
    [issue] = [i for i in ws["issues"] if i["stage"].startswith("parse.")]
    expected = next(d for d in issue["diagnostics"] if d["code"] == "unreadable_pages")
    assert [p["page"] for p in item["pages"]] == expected["detail"]["pages"]
    uri = (src / "scanned.pdf").as_uri()
    for page in item["pages"]:
        assert page["url"] == f"{uri}#page={page['page']}"
        [finding] = page["findings"]
        assert finding["code"] == "unreadable_pages"
        assert finding["reasons"] == expected["detail"]["reasons"][str(page["page"])]
    # Runs: only the ones that touched this item, never the unrelated ingest.
    assert {r["step"] for r in ws["runs"]} == {"ingest", "parse"}
    assert unrelated_ingest not in {r["id"] for r in ws["runs"]}

    mixed = _subject(cli, ws_root, "mixed.pdf")["subject_detail"]["items"][0]
    unreadable = [
        p["page"]
        for p in mixed["pages"]
        if any(f["code"] == "unreadable_pages" for f in p["findings"])
    ]
    assert unreadable == [2, 3, 6]
    [text] = mixed["texts"]
    assert text["parser"] == "pdfplumber" and text["chars"] == len(text["text"]) > 0

    # The terminal view keeps each link on one line, whatever the console width.
    human = cli("inspect", "--workspace", str(ws_root), "--subject", "mixed.pdf")
    assert human.exit_code == 0, human.stderr
    link = f"{(src / 'mixed.pdf').as_uri()}#page=2"
    assert len(link) > 80  # longer than the test console, so a wrap would split it
    assert link in [line.strip() for line in human.stdout.splitlines()]

    # A missing or changed original keeps its path but loses its links.
    (src / "scanned.pdf").unlink()
    (src / "mixed.pdf").write_bytes(b"%PDF-1.4 replaced")
    gone = _subject(cli, ws_root, "scanned.pdf")["subject_detail"]["items"][0]
    changed = _subject(cli, ws_root, "mixed.pdf")["subject_detail"]["items"][0]
    assert (gone["original_status"], changed["original_status"]) == ("missing", "changed")
    assert all(p["url"] is None for p in gone["pages"] + changed["pages"])
    assert gone["pages"] and changed["pages"]


def test_subject_detail_shows_records_and_the_items_a_group_rests_on(cli, ws_root, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name, amount in (("good", 10), ("great", 3), ("other", 5)):
        (src / f"{name}.json").write_text(
            json.dumps({"name": name, "amount": amount}), encoding="utf-8"
        )
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    for step in ("extract", "by_initial"):
        res = cli("run", "--step", step, "--workspace", str(ws_root), "--recipe", str(DEMO))
        assert res.exit_code == 0, res.output

    good = _subject(cli, ws_root, "good.json")["subject_detail"]
    [record] = good["records"]
    assert record["stage"].endswith(".extract")
    assert record["payload"] == {"name": "good", "amount": 10}

    group = _subject(cli, ws_root, "group:g")["subject_detail"]
    assert [i["locator"] for i in group["items"]] == ["good.json", "great.json"]
    assert all(i["original_path"] == str(src / i["locator"]) for i in group["items"])
    [total] = group["records"]
    assert (total["payload"]["count"], total["payload"]["total"]) == (2, 13)


def test_subject_resolution_refuses_ambiguous_and_unknown_subjects(cli, ws_root, tmp_path):
    """A spot check on the wrong file is worse than none: guess nothing, list the matches."""
    one, two = tmp_path / "one", tmp_path / "two"
    for folder in (one / "x", one / "y", one / "z", one / "w", two):
        folder.mkdir(parents=True)
    (one / "x" / "attachment_1.json").write_text("{}", encoding="utf-8")
    (one / "y" / "attachment_1.json").write_text("{}", encoding="utf-8")
    (one / "z" / "only_one.json").write_text("{}", encoding="utf-8")
    # As a LIKE pattern, "attachment_1.json" would also match this file.
    (one / "w" / "attachmentZ1.json").write_text("{}", encoding="utf-8")
    (one / "same.json").write_text("{}", encoding="utf-8")
    (two / "same.json").write_text("{}", encoding="utf-8")
    assert cli.ingest(ws_root, one)[0] == 0
    assert cli.ingest(ws_root, two)[0] == 0

    def inspect(subject):
        return cli("inspect", "--workspace", str(ws_root), "--subject", subject, "--json")

    # The same locator in two sources, and one file name in two folders: both listed, exit 2.
    same = inspect("same.json")
    assert same.exit_code == 2
    assert f"{one}/same.json" in same.stderr and f"{two}/same.json" in same.stderr
    by_name = inspect("attachment_1.json")
    assert by_name.exit_code == 2
    assert "matches 2 items" in by_name.stderr
    assert "x/attachment_1.json" in by_name.stderr and "y/attachment_1.json" in by_name.stderr
    assert "attachmentZ1.json" not in by_name.stderr  # `_` is literal, not a LIKE wildcard

    # A full locator, or a file name only one item has, resolves to that item.
    exact = json.loads(inspect("x/attachment_1.json").stdout)["workspaces"][0]
    assert [i["locator"] for i in exact["subject_detail"]["items"]] == ["x/attachment_1.json"]
    unique = json.loads(inspect("only_one.json").stdout)["workspaces"][0]
    assert [i["locator"] for i in unique["subject_detail"]["items"]] == ["z/only_one.json"]

    # Unknown subjects are errors, not empty reports.
    for unknown in ("attachmentX1.json", "nothing.json", "item:9999", "group:nope"):
        res = inspect(unknown)
        assert res.exit_code == 2, unknown
        assert "matches no item" in res.stderr
