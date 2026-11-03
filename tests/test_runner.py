"""The runner and recipe stages, driven through the CLI with tests/recipes/demo."""

import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from commissure.recipes import RecipeError, import_closure, load_recipe
from commissure.runner import RunContext
from commissure.workspace import DB_NAME, MIGRATIONS, connect, open_workspace, transaction, utc_now

DEMO = Path(__file__).parent / "recipes" / "demo"
ENTRIES = {"alpha": 5, "bravo": 3, "skipme": 1, "beta": 0, "neg": -2, "badtype": 1, "apple": 7}


def write_corpus(folder: Path, entries: dict[str, int]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, amount in entries.items():
        (folder / f"{name}.json").write_text(json.dumps({"name": name, "amount": amount}))
    return folder


def stage(cli, ws_root, step, *extra, recipe=DEMO, env=None):
    result = cli(
        "run",
        "--step",
        step,
        "--workspace",
        str(ws_root),
        "--recipe",
        str(recipe),
        "--json",
        *extra,
        env=env,
    )
    return result.exit_code, (json.loads(result.stdout) if result.stdout.strip() else {})


def q(ws_root, sql, *params):
    conn = connect(ws_root / DB_NAME)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def prepare(cli, ws_root, src):
    assert cli.ingest(ws_root, src)[0] == 0
    result = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json")
    assert result.exit_code == 0, result.output


@pytest.fixture
def demo(cli, ws_root, tmp_path):
    prepare(cli, ws_root, write_corpus(tmp_path / "src", ENTRIES))
    return tmp_path / "src"


def outcomes(ws_root, stage_name):
    rows = q(
        ws_root,
        "SELECT i.locator, co.* FROM current_outcomes co"
        " JOIN items i ON 'item:' || i.id = co.subject_key"
        " WHERE co.stage = ? ORDER BY i.locator",
        stage_name,
    )
    return {r["locator"].removesuffix(".json"): r for r in rows}


# --- outcome vocabulary ----------------------------------------------------------


def test_item_stage_outcomes_and_result_rows(cli, ws_root, demo):
    code, report = stage(cli, ws_root, "extract")
    assert code == 0, report
    assert {k: report[k] for k in ("discovered", "valid", "warning", "quarantined", "skipped")} == {
        "discovered": 7,
        "valid": 3,
        "warning": 1,
        "quarantined": 2,
        "skipped": 1,
    }
    got = {
        k: (r["outcome"], r["reason_code"], r["record_id"] is not None)
        for k, r in outcomes(ws_root, "demo.extract").items()
    }
    assert got == {
        "alpha": ("valid", None, True),
        "apple": ("valid", None, True),
        "bravo": ("valid", None, True),
        "beta": ("warning", "zero_amount", True),
        "neg": ("quarantined", "negative_amount", False),
        "badtype": ("quarantined", "schema_violation", False),
        "skipme": ("skipped", "not_applicable", False),
    }
    # Skipped is a status with a reason and no result row.
    assert q(ws_root, "SELECT count(*) AS n FROM records WHERE stage = 'demo.extract'") == [
        {"n": 4}
    ]
    run = q(ws_root, "SELECT * FROM runs WHERE id = ?", report["run_id"])[0]
    assert (run["valid"], run["warning"], run["quarantined"], run["skipped"]) == (3, 1, 2, 1)
    assert run["attempted"] == 6


def test_schema_violation_names_the_field(cli, ws_root, demo):
    stage(cli, ws_root, "extract")
    diags = json.loads(outcomes(ws_root, "demo.extract")["badtype"]["diagnostics"])
    assert [(d["code"], d["field"]) for d in diags] == [("schema_violation", "amount")]


def test_downstream_skips_explicitly_when_upstream_is_not_valid(cli, ws_root, demo):
    code, report = stage(cli, ws_root, "score")  # extract has not run yet
    assert (code, report["status"], report["skipped"], report["attempted"]) == (1, "failed", 7, 0)
    assert "nothing_attempted" in report["error"] and "upstream_missing=7" in report["error"]
    assert {r["reason_code"] for r in outcomes(ws_root, "demo.score").values()} == {
        "upstream_missing"
    }
    stage(cli, ws_root, "extract")
    stage(cli, ws_root, "score")
    got = {k: r["reason_code"] for k, r in outcomes(ws_root, "demo.score").items()}
    assert got == {
        "alpha": None,
        "apple": None,
        "bravo": None,
        "beta": None,
        "neg": "upstream_not_valid",
        "badtype": "upstream_not_valid",
        "skipme": "upstream_not_valid",
    }
    payload = q(
        ws_root,
        "SELECT payload FROM current_records WHERE stage = 'demo.score'"
        " AND subject_key = (SELECT 'item:' || id FROM items"
        " WHERE locator = 'apple.json')",
    )[0]["payload"]
    assert json.loads(payload) == {"name": "apple", "doubled": 14}
    lineage = q(
        ws_root,
        "SELECT count(*) AS n FROM outcome_inputs oi"
        " JOIN outcomes o ON o.id = oi.outcome_id WHERE o.stage = 'demo.score'",
    )
    assert lineage == [{"n": 4}]


def test_group_and_run_subjects(cli, ws_root, demo):
    stage(cli, ws_root, "extract")
    assert stage(cli, ws_root, "by_initial")[1]["valid"] == 2
    assert stage(cli, ws_root, "total")[1]["valid"] == 1
    payloads = {
        r["subject_key"]: json.loads(r["payload"])
        for r in q(
            ws_root,
            "SELECT subject_key, payload FROM current_records"
            " WHERE stage IN ('demo.by_initial', 'demo.total')",
        )
    }
    assert payloads == {
        "group:a": {"key": "a", "count": 2, "total": 12, "excluded": []},
        "group:b": {"key": "b", "count": 2, "total": 3, "excluded": []},
        "run": {
            "key": "run",
            "count": 4,
            "total": 15,
            "excluded": [
                "badtype.json:upstream_not_valid",
                "neg.json:upstream_not_valid",
                "skipme.json:upstream_not_valid",
            ],
        },
    }


def test_unhandled_exception_fails_the_run_and_keeps_checkpoints(cli, ws_root, tmp_path):
    entries = {f"ok{i}": i + 1 for i in range(6)} | {"boom": 1}
    prepare(cli, ws_root, write_corpus(tmp_path / "src", entries))
    code, report = stage(cli, ws_root, "extract", "--workers", "1")
    assert (code, report["status"]) == (1, "failed")
    assert "RuntimeError" in report["error"]
    run = q(ws_root, "SELECT remaining FROM runs WHERE id = ?", report["run_id"])[0]
    committed = q(ws_root, "SELECT count(*) AS n FROM outcomes WHERE run_id = ?", report["run_id"])[
        0
    ]["n"]
    assert committed + run["remaining"] == 7 and run["remaining"] >= 1


# --- the definition of done ------------------------------------------------------


def _spawn(ws_root, *args):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from commissure.cli import app; app()",
            "run",
            "--step",
            "extract",
            "--workspace",
            str(ws_root),
            "--recipe",
            str(DEMO),
            "--workers",
            "2",
            "--param",
            "delay=0.15",
            "--json",
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_outcomes(ws_root, at_least, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            n = q(ws_root, "SELECT count(*) AS n FROM outcomes WHERE stage = 'demo.extract'")[0][
                "n"
            ]
        except sqlite3.OperationalError:
            n = 0
        if n >= at_least:
            return n
        time.sleep(0.02)
    raise AssertionError("run never reached the expected checkpoint")


@pytest.fixture
def twelve(cli, ws_root, tmp_path):
    prepare(cli, ws_root, write_corpus(tmp_path / "src", {f"n{i:02d}": i + 1 for i in range(12)}))


def _assert_one_outcome_per_subject(ws_root, chain):
    marks = ",".join("?" * len(chain))
    rows = q(
        ws_root,
        f"SELECT subject_key, count(*) AS n FROM outcomes"
        f" WHERE stage = 'demo.extract' AND run_id IN ({marks})"
        f" GROUP BY subject_key",
        *chain,
    )
    assert len(rows) == 12 and all(r["n"] == 1 for r in rows)


def test_killed_run_resumes_without_loss_or_double_count(cli, ws_root, twelve):
    proc = _spawn(ws_root)
    before_kill = _wait_for_outcomes(ws_root, 3)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    committed = q(ws_root, "SELECT count(*) AS n FROM outcomes WHERE stage = 'demo.extract'")[0][
        "n"
    ]
    assert 3 <= before_kill <= committed < 12

    code, report = stage(cli, ws_root, "extract", "--param", "delay=0.15")
    assert (code, report["status"]) == (0, "succeeded")
    first = q(
        ws_root, "SELECT id, status, error FROM runs WHERE step = 'demo.extract' ORDER BY id"
    )[0]
    assert first["status"] == "failed" and first["error"].startswith("abandoned")
    assert report["resumed_run_id"] == first["id"]
    assert report["unchanged"] == committed
    assert report["valid"] == 12 - committed
    _assert_one_outcome_per_subject(ws_root, [first["id"], report["run_id"]])


def test_ctrl_c_leaves_the_database_consistent(cli, ws_root, twelve):
    proc = _spawn(ws_root)
    _wait_for_outcomes(ws_root, 3)
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=20)
    run = q(ws_root, "SELECT * FROM runs WHERE step = 'demo.extract'")[0]
    assert run["status"] == "failed" and "KeyboardInterrupt" in run["error"]
    assert q(ws_root, "PRAGMA integrity_check") == [{"integrity_check": "ok"}]
    with_rows = q(
        ws_root,
        "SELECT count(*) AS n FROM outcomes WHERE stage = 'demo.extract' AND record_id IS NOT NULL",
    )[0]["n"]
    records = q(ws_root, "SELECT count(*) AS n FROM records WHERE stage = 'demo.extract'")[0]["n"]
    assert with_rows == records  # no orphan payloads, no outcome without its payload
    committed = q(ws_root, "SELECT count(*) AS n FROM outcomes")[0]["n"] - 12  # minus parse
    assert committed + run["remaining"] == 12

    code, report = stage(cli, ws_root, "extract", "--param", "delay=0.15")
    assert (code, report["resumed_run_id"], report["unchanged"]) == (0, run["id"], committed)
    _assert_one_outcome_per_subject(ws_root, [run["id"], report["run_id"]])


def test_changing_one_stage_marks_only_its_outputs_stale(cli, ws_root, demo, tmp_path):
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    for s in ("extract", "score", "total"):
        assert stage(cli, ws_root, s, recipe=recipe)[0] == 0
    extract_records = q(ws_root, "SELECT id FROM records WHERE stage = 'demo.extract'")

    def stale():
        rows = q(
            ws_root,
            "SELECT stage, max(code_stale) AS s FROM current_outcomes"
            " WHERE stage LIKE 'demo.%' GROUP BY stage ORDER BY stage",
        )
        return {r["stage"]: r["s"] for r in rows}

    assert stale() == {"demo.extract": 0, "demo.score": 0, "demo.total": 0}
    score_py = recipe / "score.py"
    score_py.write_text(score_py.read_text().replace("entry.amount * 2", "entry.amount * 3"))
    load_and_register(ws_root, recipe)
    assert stale() == {"demo.extract": 0, "demo.score": 1, "demo.total": 0}

    # A non-code file changes the recipe fingerprint but no stage fingerprint.
    (recipe / "notes.md").write_text("edited")
    load_and_register(ws_root, recipe)
    assert stale() == {"demo.extract": 0, "demo.score": 1, "demo.total": 0}

    # Rerunning score clears it and leaves extract's work untouched.
    stage(cli, ws_root, "score", recipe=recipe)
    assert stale() == {"demo.extract": 0, "demo.score": 0, "demo.total": 0}
    assert q(ws_root, "SELECT id FROM records WHERE stage = 'demo.extract'") == extract_records

    # A shared module (the schema) reaches every stage that imports it.
    schema_py = recipe / "schema.py"
    schema_py.write_text(schema_py.read_text() + "\n# touched\n")
    load_and_register(ws_root, recipe)
    assert stale() == {"demo.extract": 1, "demo.score": 1, "demo.total": 1}


def test_callable_in_recipe_py_does_not_stale_sibling_stages(cli, ws_root, demo, tmp_path):
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    recipe_py = recipe / "recipe.py"
    recipe_py.write_text(
        recipe_py.read_text()
        .replace(
            "STAGES = [",
            "MAX_Q = 0\n"
            "def inline_gate(counts: dict[str, int]) -> list[str]:\n"
            "    return ['quarantined'] if counts['quarantined'] > MAX_Q else []\n\n"
            "STAGES = [",
        )
        .replace(
            '        params={"delay": 0},\n        checks=(',
            '        params={"delay": 0},\n        gate=inline_gate,\n        checks=(',
        )
    )
    assert "gate=inline_gate" in recipe_py.read_text()  # the patch above took
    # Remove negative items so inline_gate passes on extract.
    (demo / "neg.json").unlink()
    (demo / "badtype.json").unlink()
    prepare(cli, ws_root, demo)
    for s in ("extract", "score", "total"):
        assert stage(cli, ws_root, s, recipe=recipe)[0] == 0

    def stale():
        rows = q(
            ws_root,
            "SELECT stage, max(code_stale) AS s FROM current_outcomes"
            " WHERE stage LIKE 'demo.%' GROUP BY stage ORDER BY stage",
        )
        return {r["stage"]: r["s"] for r in rows}

    assert stale() == {"demo.extract": 0, "demo.score": 0, "demo.total": 0}

    # Editing score.py (imported by recipe.py) must not mark extract or total code_stale.
    score_py = recipe / "score.py"
    score_py.write_text(score_py.read_text().replace("entry.amount * 2", "entry.amount * 3"))
    load_and_register(ws_root, recipe)
    assert stale() == {"demo.extract": 0, "demo.score": 1, "demo.total": 0}

    # Editing the inline gate constant in recipe.py marks only extract code_stale.
    recipe_py.write_text(recipe_py.read_text().replace("MAX_Q = 0", "MAX_Q = 1"))
    load_and_register(ws_root, recipe)
    assert stale() == {"demo.extract": 1, "demo.score": 1, "demo.total": 0}


def load_and_register(ws_root, recipe):
    from commissure.recipes import register_stages

    ws = open_workspace(ws_root)
    register_stages(ws.conn, load_recipe(recipe))
    ws.close()


# --- staleness labels ------------------------------------------------------------


def test_identical_upstream_rerun_does_not_stale_downstream(cli, ws_root, demo):
    stage(cli, ws_root, "extract")
    stage(cli, ws_root, "score")
    assert stage(cli, ws_root, "extract")[1]["status"] == "succeeded"  # a fresh snapshot
    labels = {r["input_stale"] for r in outcomes(ws_root, "demo.score").values()}
    assert labels == {0}


def test_changed_upstream_marks_downstream_and_falls_back_visibly(cli, ws_root, demo):
    stage(cli, ws_root, "extract")
    stage(cli, ws_root, "score")
    (demo / "alpha.json").write_text(json.dumps({"name": "alpha", "amount": -9}))
    prepare(cli, ws_root, demo)
    stage(cli, ws_root, "extract")

    alpha_score = outcomes(ws_root, "demo.score")["alpha"]
    assert (alpha_score["version_stale"], alpha_score["input_stale"]) == (1, 1)
    assert outcomes(ws_root, "demo.score")["bravo"]["input_stale"] == 0

    fallback = q(
        ws_root,
        "SELECT fallback, latest_outcome FROM current_records"
        " WHERE stage = 'demo.extract' AND subject_key = ("
        "SELECT 'item:' || id FROM items WHERE locator = 'alpha.json')",
    )
    assert fallback == [{"fallback": 1, "latest_outcome": "quarantined"}]
    stage(cli, ws_root, "score")
    assert outcomes(ws_root, "demo.score")["alpha"]["reason_code"] == "upstream_not_valid"


# --- resume rules ----------------------------------------------------------------


def test_limit_then_resume_fresh_and_parameter_changes(cli, ws_root, demo):
    code, first = stage(cli, ws_root, "extract", "--limit", "3")
    assert (code, first["remaining"]) == (0, 4)
    code, second = stage(cli, ws_root, "extract")
    assert (second["resumed_run_id"], second["unchanged"], second["remaining"]) == (
        first["run_id"],
        3,
        0,
    )
    code, third = stage(cli, ws_root, "extract", "--limit", "2")
    assert third["resumed_run_id"] is None and third["unchanged"] == 0  # complete: fresh
    code, fourth = stage(cli, ws_root, "extract", "--fresh")
    assert fourth["not_resumed"].startswith("--fresh")
    assert q(ws_root, "SELECT override FROM runs WHERE id = ?", fourth["run_id"]) == [
        {"override": "fresh"}
    ]
    stage(cli, ws_root, "extract", "--limit", "1")
    code, changed = stage(cli, ws_root, "extract", "--param", "delay=0.001")
    assert "parameters changed" in changed["not_resumed"] and changed["unchanged"] == 0


# --- fail-closed defaults and the recipe gate ------------------------------------


def test_fail_closed_defaults(cli, ws_root, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    cli.ingest(ws_root, empty, "--allow-empty")
    code, report = stage(cli, ws_root, "extract")
    assert (code, report["status"]) == (1, "failed") and "EmptyDiscovery" in report["error"]
    assert stage(cli, ws_root, "extract", "--allow-empty")[1]["status"] == "noop"

    ws_quarantined = tmp_path / "ws_quarantined"
    prepare(cli, ws_quarantined, write_corpus(tmp_path / "bad", {"n1": -1, "n2": -2}))
    code, report = stage(cli, ws_quarantined, "extract")
    assert (code, report["quarantined"]) == (1, 2) and "AllQuarantined" in report["error"]

    ws_skipped = tmp_path / "ws_skipped"
    prepare(cli, ws_skipped, write_corpus(tmp_path / "skip_only", {"skip1": 1, "skip2": 2}))
    code, report = stage(cli, ws_skipped, "extract")
    assert (
        code,
        report["status"],
        report["discovered"],
        report["attempted"],
        report["skipped"],
    ) == (
        1,
        "failed",
        2,
        0,
        2,
    )
    assert "nothing_attempted" in report["error"] and "not_applicable=2" in report["error"]
    run_row = q(
        ws_skipped,
        "SELECT status, attempted, skipped, error FROM runs WHERE id = ?",
        report["run_id"],
    )[0]
    assert (run_row["status"], run_row["attempted"], run_row["skipped"]) == ("failed", 0, 2)
    assert "nothing_attempted" in run_row["error"] and "not_applicable=2" in run_row["error"]

    relaxed = _recipe(
        tmp_path,
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Diagnostic, RecordEnvelope, Stage\n"
        "class M(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    x: int\n"
        "def _skip(c, i):\n"
        "    return RecordEnvelope.skipped('skip_reason', Diagnostic(code='skip_reason'))\n"
        "STAGES = [Stage('s', _skip, schema=M, allow_nothing_attempted=True)]\n",
        name="relaxed",
    )
    code, report = stage(cli, ws_skipped, "s", recipe=relaxed)
    assert (code, report["status"], report["attempted"], report["skipped"]) == (
        0,
        "succeeded",
        0,
        2,
    )


def test_recipe_gate_judges_the_complete_snapshot(cli, ws_root, demo):
    code, report = stage(cli, ws_root, "strict_extract", "--limit", "2")
    assert (code, report["gate"]) == (0, "deferred")
    code, report = stage(cli, ws_root, "strict_extract")
    assert (code, report["gate"]) == (1, "failed")
    assert "GateFailed: 2 quarantined" in report["error"]


# --- recipe loading --------------------------------------------------------------


def _recipe(tmp_path, body, name="r"):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "recipe.py").write_text(body)
    return folder


@pytest.mark.parametrize(
    "body, message",
    [
        ("STAGES = []", "non-empty list"),
        (
            "from pydantic import BaseModel\nfrom commissure.api import Stage\n"
            "class M(BaseModel):\n    x: int\n"
            "STAGES = [Stage('a', lambda c, i: M(x=1), schema=M)]",
            "extra='forbid'",
        ),
        (
            "from pydantic import BaseModel, ConfigDict\nfrom commissure.api import Stage\n"
            "class M(BaseModel):\n    model_config = ConfigDict(extra='forbid')\n    x: int\n"
            "STAGES = [Stage('a', lambda c, i: M(x=1), schema=M, needs=('zz',))]",
            "earlier item stage",
        ),
        (
            "from pydantic import BaseModel, ConfigDict\nfrom commissure.api import Stage\n"
            "class M(BaseModel):\n    model_config = ConfigDict(extra='forbid')\n    x: int\n"
            "STAGES = [Stage('a', lambda c, i: M(x=1), schema=M, assets=('missing.docx',))]",
            "missing asset",
        ),
        ("raise ImportError('nope')", "loading"),
    ],
)
def test_bad_recipes_fail_at_startup(cli, ws_root, tmp_path, body, message):
    recipe = _recipe(tmp_path, body)
    result = cli("run", "--step", "a", "--workspace", str(ws_root), "--recipe", str(recipe))
    assert result.exit_code == 2 and message in result.stderr
    assert not ws_root.exists()


def test_recipe_usage_errors(cli, ws_root, demo, tmp_path):
    assert stage(cli, ws_root, "nope")[0] == 2
    assert stage(cli, ws_root, "extract", "--parser", "pdfium")[0] == 2
    assert cli("run", "--step", "extract", "--workspace", str(ws_root)).exit_code == 2
    assert cli("run", "--step", "parse", "--workspace", str(ws_root), "--fresh").exit_code == 2
    with pytest.raises(RecipeError, match="no recipe.py"):
        load_recipe(tmp_path)


def test_stage_fingerprints_follow_relative_imports():
    closure = {p.name for p in import_closure(DEMO, [DEMO / "score.py"])}
    assert closure == {"score.py", "schema.py"}
    recipe = load_recipe(DEMO)
    assert len(set(recipe.stage_fps.values())) == len(recipe.stages)
    assert recipe.stage_fps["extract"] != recipe.stage_fps["strict_extract"]  # gate differs


def test_registered_stages(cli, ws_root, demo):
    stage(cli, ws_root, "extract")
    rows = q(ws_root, "SELECT stage FROM stages ORDER BY stage")
    assert [r["stage"] for r in rows] == [
        "demo.by_initial",
        "demo.carry",
        "demo.cite",
        "demo.extract",
        "demo.score",
        "demo.strict_extract",
        "demo.total",
    ]


# --- ctx.map ---------------------------------------------------------------------


def test_ctx_map_is_bounded_and_ordered():
    active, peak, lock = 0, 0, threading.Lock()

    def work(x):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return x * 2

    ctx = RunContext(run_id=1, stage="s", params={}, workers=3)
    assert ctx.map(work, range(12)) == [x * 2 for x in range(12)]
    assert peak <= 3 and peak > 1
    nested = ctx.map(lambda _: ctx.map(work, range(4)), range(3))
    assert nested == [[0, 2, 4, 6]] * 3 and peak <= 3


# --- migration -------------------------------------------------------------------


def test_migration_from_version_2_moves_parse_outcomes(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    conn = connect(root / DB_NAME)
    for i, script in enumerate(MIGRATIONS[:2], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {i};\nCOMMIT;")
    now = utc_now()
    conn.executescript(f"""
        INSERT INTO sources (kind, uri, role, created_at)
            VALUES ('folder', '/x', 'corpus', '{now}');
        INSERT INTO runs (step, trigger, status, started_at, config)
            VALUES ('parse', 'manual', 'succeeded', '{now}', '{{}}');
        INSERT INTO items (source_id, locator) VALUES (1, 'a.pdf');
        INSERT INTO item_versions (item_id, run_id, bytes_hash, size_bytes, role, is_current,
            created_at) VALUES (1, 1, 'bh', 1, 'corpus', 1, '{now}');
        INSERT INTO extracted_text (item_version_id, parser, parser_version, text, text_hash,
            run_id, created_at) VALUES (1, 'pdfplumber', 'v1', 'hello', 'th', 1, '{now}');
        INSERT INTO item_outcomes (run_id, stage, item_version_id, tool, tool_version, outcome,
            extracted_text_id, created_at) VALUES (1, 'parse', 1, 'pdfplumber', 'v1', 'valid', 1,
            '{now}');
    """)
    conn.close()
    ws = open_workspace(root)
    rows = [
        dict(r)
        for r in ws.conn.execute(
            "SELECT stage, subject_key, input_fp, stage_fp, params, outcome FROM outcomes"
        )
    ]
    assert rows == [
        {
            "stage": "parse.pdfplumber",
            "subject_key": "item:1",
            "input_fp": "bh",
            "stage_fp": "v1",
            "params": "{}",
            "outcome": "valid",
        }
    ]
    assert [dict(r) for r in ws.conn.execute("SELECT parser, stale FROM current_text")] == [
        {"parser": "pdfplumber", "stale": 0}
    ]
    ws.close()


# --- gaps found while planning the fail-first pass --------------------------------


def test_resume_refused_when_any_recipe_file_changed(cli, ws_root, demo, tmp_path):
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    stage(cli, ws_root, "extract", "--limit", "1", recipe=recipe)
    (recipe / "notes.md").write_text("a template or prompt changed")
    code, report = stage(cli, ws_root, "extract", recipe=recipe)
    assert "recipe changed" in report["not_resumed"] and report["unchanged"] == 0


def test_resume_recomputes_a_subject_whose_input_changed(cli, ws_root, demo):
    stage(cli, ws_root, "extract", "--limit", "3")  # alpha, apple, badtype by item order
    first = "alpha.json"
    (demo / first).write_text(json.dumps({"name": first.removesuffix(".json"), "amount": 42}))
    prepare(cli, ws_root, demo)
    code, report = stage(cli, ws_root, "extract")
    assert report["resumed_run_id"] is not None and report["unchanged"] == 2
    entry = q(
        ws_root,
        "SELECT payload FROM current_records WHERE stage = 'demo.extract'"
        " AND subject_key = (SELECT 'item:' || id FROM items WHERE locator = ?)",
        first,
    )[0]
    assert json.loads(entry["payload"])["amount"] == 42


def test_upstream_from_an_older_item_version_is_not_consumed(cli, ws_root, demo):
    stage(cli, ws_root, "extract")
    (demo / "bravo.json").write_text(json.dumps({"name": "bravo", "amount": 30}))
    prepare(cli, ws_root, demo)
    stage(cli, ws_root, "score")  # extract not rerun: its bravo record is for the old version
    assert outcomes(ws_root, "demo.score")["bravo"]["reason_code"] == "upstream_out_of_date"


def test_items_without_text_are_skipped(cli, ws_root, tmp_path):
    src = write_corpus(tmp_path / "src", {"alpha": 1})
    (src / "readme.txt").write_text("no parser for this type")
    prepare(cli, ws_root, src)
    stage(cli, ws_root, "extract")
    rows = q(
        ws_root,
        "SELECT i.locator, o.outcome, o.reason_code FROM outcomes o"
        " JOIN items i ON 'item:' || i.id = o.subject_key"
        " WHERE o.stage = 'demo.extract' ORDER BY i.locator",
    )
    assert [(r["locator"], r["outcome"], r["reason_code"]) for r in rows] == [
        ("alpha.json", "valid", None),
        ("readme.txt", "skipped", "no_text"),
    ]


def test_subjects_run_concurrently(cli, ws_root, twelve):
    started = time.monotonic()
    code, _ = stage(cli, ws_root, "extract", "--workers", "4", "--param", "delay=0.15")
    elapsed = time.monotonic() - started
    assert code == 0 and elapsed < 1.3  # sequential would take at least 12 x 0.15 = 1.8s


def test_envelope_shapes_are_enforced():
    from pydantic import BaseModel, ValidationError

    from commissure.records import Diagnostic, RecordEnvelope

    class P(BaseModel):
        x: int

    for bad in (
        dict(outcome="valid", payload=P(x=1), reason_code="r"),
        dict(outcome="valid"),
        dict(outcome="warning", payload=P(x=1), reason_code="r"),
        dict(outcome="warning", payload=P(x=1), reason_code="", diagnostics=[Diagnostic(code="x")]),
        dict(outcome="skipped", payload=P(x=1), reason_code="r"),
        dict(outcome="skipped", reason_code=""),
        dict(outcome="quarantined"),
        dict(outcome="quarantined", reason_code=""),
        dict(outcome="lost", reason_code="r"),
    ):
        with pytest.raises(ValidationError):
            RecordEnvelope(**bad)
    ok = RecordEnvelope.warning(P(x=1), "odd", Diagnostic(code="odd", field="x"))
    assert ok.outcome == "warning" and ok.diagnostics[0].field == "x"


def test_resume_recomputes_when_text_or_upstream_changed_without_new_bytes(cli, ws_root, demo):
    def alpha(step):  # alpha.json: in the first --limit slice, valid in both stages
        return q(
            ws_root,
            "SELECT subject_key, item_version_id FROM outcomes WHERE stage = ?"
            " AND subject_key = (SELECT 'item:' || id FROM items WHERE locator = 'alpha.json')",
            step,
        )[0]

    def write(sql, *params):
        conn = connect(ws_root / DB_NAME)
        try:
            with transaction(conn):
                conn.execute(sql, params)
        finally:
            conn.close()

    # (a) A parser upgrade changes an item's text while its bytes stay the same.
    stage(cli, ws_root, "extract", "--limit", "3")
    item = alpha("demo.extract")
    text_id = q(
        ws_root, "SELECT id FROM extracted_text WHERE item_version_id = ?", item["item_version_id"]
    )[0]["id"]
    old = q(ws_root, "SELECT text FROM extracted_text WHERE id = ?", text_id)[0]["text"]
    data = json.loads(old) | {"amount": 77}
    write(
        "INSERT INTO extracted_text (item_version_id, parser, parser_version, text, text_hash,"
        " run_id, created_at) VALUES (?, 'json', 'newer', ?, 'changed', 1, ?)",
        item["item_version_id"],
        json.dumps(data),
        utc_now(),
    )
    new_text = q(ws_root, "SELECT max(id) AS id FROM extracted_text")[0]["id"]
    write(
        "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key, item_version_id,"
        " input_fp, stage_fp, params, outcome, extracted_text_id, created_at) VALUES"
        " (1, 'parse.json', 'item', ?, ?, 'b', 'newer', '{}', 'valid', ?, ?)",
        item["subject_key"],
        item["item_version_id"],
        new_text,
        utc_now(),
    )
    code, report = stage(cli, ws_root, "extract")
    assert (report["resumed_run_id"] is not None, report["unchanged"]) == (True, 2)
    payload = q(
        ws_root,
        "SELECT payload FROM current_records WHERE stage = 'demo.extract' AND subject_key = ?",
        item["subject_key"],
    )[0]["payload"]
    assert json.loads(payload)["amount"] == 77

    # (b) An upstream payload changes while the item's bytes and text stay the same.
    stage(cli, ws_root, "score", "--limit", "3")
    item = alpha("demo.score")
    write(
        "INSERT INTO records (stage, payload, payload_hash, created_at)"
        " VALUES ('demo.extract', ?, 'changed', ?)",
        json.dumps({"name": "x", "amount": 99}),
        utc_now(),
    )
    record = q(ws_root, "SELECT max(id) AS id FROM records")[0]["id"]
    write(
        "INSERT INTO outcomes (run_id, stage, subject_kind, subject_key, item_version_id,"
        " input_fp, stage_fp, params, outcome, record_id, created_at) VALUES"
        " (1, 'demo.extract', 'item', ?, ?, 'i', 's', '{}', 'valid', ?, ?)",
        item["subject_key"],
        item["item_version_id"],
        record,
        utc_now(),
    )
    code, report = stage(cli, ws_root, "score")
    assert (report["resumed_run_id"] is not None, report["unchanged"]) == (True, 2)
    payload = q(
        ws_root,
        "SELECT payload FROM current_records WHERE stage = 'demo.score' AND subject_key = ?",
        item["subject_key"],
    )[0]["payload"]
    assert json.loads(payload)["doubled"] == 198


def test_run_context_outputs_is_under_outputs_and_unique_per_run(cli, ws_root, demo, tmp_path):
    recipe = _recipe(
        tmp_path,
        "from pathlib import Path\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n"
        "class Out(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    path: str\n"
        "def noop(ctx, item):\n"
        "    return Out(path='')\n"
        "def render(ctx, group):\n"
        "    target = ctx.outputs / 'summary.txt'\n"
        "    target.write_text(f'items={len(group.items)}\\n')\n"
        "    return Out(path=str(target))\n"
        "STAGES = [\n"
        "    Stage('noop', noop, schema=Out),\n"
        "    Stage('render', render, schema=Out, subject='run'),\n"
        "]\n",
    )
    assert stage(cli, ws_root, "noop", recipe=recipe)[0] == 0
    assert list((ws_root / "outputs").iterdir()) == []  # created only on first access

    code1, r1 = stage(cli, ws_root, "render", recipe=recipe)
    code2, r2 = stage(cli, ws_root, "render", recipe=recipe)
    assert (code1, code2) == (0, 0)
    rows = q(
        ws_root,
        "SELECT run_id, payload FROM outcomes JOIN records ON records.id = record_id"
        " WHERE outcomes.stage = 'r.render' ORDER BY outcomes.id",
    )
    p1 = Path(json.loads(rows[0]["payload"])["path"])
    p2 = Path(json.loads(rows[1]["payload"])["path"])
    assert p1 != p2
    assert p1.is_relative_to(ws_root / "outputs") and p2.is_relative_to(ws_root / "outputs")
    assert p1.parent == ws_root / "outputs" / "r.render" / str(r1["run_id"])
    assert p2.parent == ws_root / "outputs" / "r.render" / str(r2["run_id"])
    assert p1.is_file() and p1.stat().st_size > 0
    assert p2.is_file() and p2.stat().st_size > 0


def test_group_stage_dropping_a_key_leaves_no_current_row(cli, ws_root, demo):
    stage(cli, ws_root, "extract")
    assert stage(cli, ws_root, "by_initial")[0] == 0
    assert [
        r["subject_key"]
        for r in q(
            ws_root,
            "SELECT subject_key FROM current_outcomes WHERE stage = 'demo.by_initial'"
            " ORDER BY subject_key",
        )
    ] == ["group:a", "group:b"]

    (demo / "bravo.json").unlink()
    (demo / "beta.json").unlink()
    prepare(cli, ws_root, demo)
    stage(cli, ws_root, "extract")
    assert stage(cli, ws_root, "by_initial")[0] == 0

    cur_outcomes = q(
        ws_root,
        "SELECT subject_key FROM current_outcomes WHERE stage = 'demo.by_initial'"
        " ORDER BY subject_key",
    )
    cur_records = q(
        ws_root,
        "SELECT subject_key FROM current_records WHERE stage = 'demo.by_initial'"
        " ORDER BY subject_key",
    )
    assert [r["subject_key"] for r in cur_outcomes] == ["group:a"]
    assert [r["subject_key"] for r in cur_records] == ["group:a"]
    # Historical rows from the first snapshot stay in the append-only outcomes table.
    all_keys = q(
        ws_root,
        "SELECT DISTINCT subject_key FROM outcomes WHERE stage = 'demo.by_initial'"
        " ORDER BY subject_key",
    )
    assert [r["subject_key"] for r in all_keys] == ["group:a", "group:b"]


def test_stage_assets_stale_only_declaring_stage(cli, ws_root, demo, tmp_path):
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    (recipe / "template.txt").write_text("v1")
    recipe_py = recipe / "recipe.py"
    recipe_py.write_text(
        recipe_py.read_text().replace(
            'Stage("total", total, schema=Total, subject="run", needs=("extract",))',
            'Stage("total", total, schema=Total, subject="run", needs=("extract",),'
            ' assets=("template.txt",))',
        )
    )
    for s in ("extract", "score", "total"):
        assert stage(cli, ws_root, s, recipe=recipe)[0] == 0

    def stale():
        rows = q(
            ws_root,
            "SELECT stage, max(code_stale) AS s FROM current_outcomes"
            " WHERE stage LIKE 'demo.%' GROUP BY stage ORDER BY stage",
        )
        return {r["stage"]: r["s"] for r in rows}

    assert stale() == {"demo.extract": 0, "demo.score": 0, "demo.total": 0}
    (recipe / "notes.md").write_text("undeclared file edited")
    load_and_register(ws_root, recipe)
    assert stale() == {"demo.extract": 0, "demo.score": 0, "demo.total": 0}

    (recipe / "template.txt").write_text("v2")
    load_and_register(ws_root, recipe)
    assert stale() == {"demo.extract": 0, "demo.score": 0, "demo.total": 1}


def test_item_exposes_source_and_upstream_outcomes(cli, ws_root, demo, tmp_path):
    from commissure.api import Source, UpstreamOutcome

    assert Source and UpstreamOutcome
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    (recipe / "audit.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "class Audit(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    source_name: str\n"
        "    source_uri: str\n"
        "    up_outcome: str\n"
        "    up_reason: str | None\n"
        "    up_diag_codes: list[str]\n"
        "def audit(ctx, item):\n"
        "    uo = item.upstream_outcomes['extract']\n"
        "    return Audit(\n"
        "        source_name=item.source.name,\n"
        "        source_uri=item.source.uri,\n"
        "        up_outcome=uo.outcome,\n"
        "        up_reason=uo.reason_code,\n"
        "        up_diag_codes=[d.code for d in uo.diagnostics],\n"
        "    )\n"
    )
    recipe_py = recipe / "recipe.py"
    recipe_py.write_text(
        recipe_py.read_text()
        + "\nfrom .audit import Audit, audit\n"
        + "STAGES.append(Stage('audit', audit, schema=Audit, needs=('extract',)))\n"
    )
    assert stage(cli, ws_root, "extract", recipe=recipe)[0] == 0
    assert stage(cli, ws_root, "audit", recipe=recipe)[0] == 0
    rows = {
        r["locator"].removesuffix(".json"): json.loads(r["payload"])
        for r in q(
            ws_root,
            "SELECT i.locator, cr.payload FROM current_records cr"
            " JOIN items i ON 'item:' || i.id = cr.subject_key"
            " WHERE cr.stage = 'demo.audit'",
        )
    }
    assert rows["alpha"] == {
        "source_name": "src",
        "source_uri": str(demo.resolve()),
        "up_outcome": "valid",
        "up_reason": None,
        "up_diag_codes": [],
    }
    assert rows["beta"] == {
        "source_name": "src",
        "source_uri": str(demo.resolve()),
        "up_outcome": "warning",
        "up_reason": "zero_amount",
        "up_diag_codes": ["zero_amount"],
    }


def test_cli_param_overrides_stage_params_and_tracks_staleness(cli, ws_root, demo, tmp_path):
    recipe = _recipe(
        tmp_path,
        "from typing import Any\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n"
        "class Echo(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    params: dict[str, Any]\n"
        "STAGES = [Stage('echo', lambda ctx, item: Echo(params=dict(ctx.params)), schema=Echo,\n"
        "                params={'mult': 2, 'tag': 'default', 'flag': False})]\n",
    )
    code, report = stage(
        cli,
        ws_root,
        "echo",
        "--param",
        "mult=5",
        "--param",
        "tag=2026-03",
        "--param",
        "flag=true",
        recipe=recipe,
    )
    assert code == 0
    run_row = q(ws_root, "SELECT config, params FROM runs WHERE id = ?", report["run_id"])[0]
    config = json.loads(run_row["config"])
    assert config["param"]["from"] == "commandline"
    expected = {"flag": True, "mult": 5, "tag": "2026-03"}
    assert json.loads(run_row["params"]) == expected
    sample = q(ws_root, "SELECT payload, params_stale FROM current_records WHERE stage = 'r.echo'")[
        0
    ]
    assert json.loads(sample["payload"])["params"] == expected
    assert sample["params_stale"] == 0

    # A partial run with a different --param leaves un-updated items params_stale.
    code, _ = stage(cli, ws_root, "echo", "--limit", "1", "--param", "mult=10", recipe=recipe)
    assert code == 0
    stale_counts = {
        r["params_stale"]: r["n"]
        for r in q(
            ws_root,
            "SELECT params_stale, count(*) AS n FROM current_outcomes"
            " WHERE stage = 'r.echo' GROUP BY params_stale",
        )
    }
    assert stale_counts == {0: 1, 1: 6}

    # Usage errors on ingest, parse, and malformed KEY=VALUE.
    assert cli.ingest(ws_root, demo, "--param", "x=1")[0] == 2
    assert (
        cli("run", "--step", "parse", "--workspace", str(ws_root), "--param", "x=1").exit_code == 2
    )
    assert stage(cli, ws_root, "echo", "--param", "no_equals", recipe=recipe)[0] == 2
    assert stage(cli, ws_root, "echo", "--param", "=emptykey", recipe=recipe)[0] == 2


def test_run_stage_reads_group_stage_records(cli, ws_root, demo, tmp_path):
    recipe = _recipe(
        tmp_path,
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Diagnostic, RecordEnvelope, Stage\n"
        "class G(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    count: int\n"
        "class R(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    by_group: dict[str, int]\n"
        "    outcomes: dict[str, str]\n"
        "def _grp(ctx, g):\n"
        "    p = G(count=len(g.items))\n"
        "    if g.key == 'b':\n"
        "        return RecordEnvelope.warning(p, 'b_warn', Diagnostic(code='b_warn'))\n"
        "    return p\n"
        "def _run(ctx, g):\n"
        "    return R(\n"
        "        by_group={k: v.count for k, v in g.upstream['grp'].items()},\n"
        "        outcomes={k: v.outcome for k, v in g.upstream_outcomes['grp'].items()},\n"
        "    )\n"
        "STAGES = [\n"
        "    Stage('grp', _grp, schema=G, subject='group', text='default',\n"
        "          group_by=lambda i: i.locator[0]),\n"
        "    Stage('roll', _run, schema=R, subject='run', needs=('grp',)),\n"
        "]\n",
    )
    assert stage(cli, ws_root, "grp", recipe=recipe)[0] == 0
    assert stage(cli, ws_root, "roll", recipe=recipe)[0] == 0
    rec = json.loads(
        q(ws_root, "SELECT payload FROM current_records WHERE stage = 'r.roll'")[0]["payload"]
    )
    assert rec["by_group"] == {"a": 2, "b": 3, "n": 1, "s": 1}
    assert rec["outcomes"]["a"] == "valid"
    assert rec["outcomes"]["b"] == "warning"


def test_group_stage_reads_only_its_own_key(cli, ws_root, demo, tmp_path):
    recipe = _recipe(
        tmp_path,
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n"
        "class G1(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    count: int\n"
        "class G2(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    seen_keys: list[str]\n"
        "    doubled: int\n"
        "    up_outcome: str\n"
        "def _g2(ctx, g):\n"
        "    up = g.upstream['g1']\n"
        "    uo = g.upstream_outcomes['g1']\n"
        "    return G2(\n"
        "        seen_keys=sorted(up.keys()),\n"
        "        doubled=up[g.key].count * 2,\n"
        "        up_outcome=uo[g.key].outcome,\n"
        "    )\n"
        "STAGES = [\n"
        "    Stage('g1', lambda ctx, g: G1(count=len(g.items)), schema=G1, subject='group',\n"
        "          text='default', group_by=lambda i: i.locator[0]),\n"
        "    Stage('g2', _g2, schema=G2, subject='group', needs=('g1',),\n"
        "          group_by=lambda i: i.locator[0]),\n"
        "]\n",
    )
    assert stage(cli, ws_root, "g1", recipe=recipe)[0] == 0
    assert stage(cli, ws_root, "g2", recipe=recipe)[0] == 0
    rows = {
        r["subject_key"]: json.loads(r["payload"])
        for r in q(
            ws_root,
            "SELECT subject_key, payload FROM current_records WHERE stage = 'r.g2'",
        )
    }
    assert rows["group:a"] == {"seen_keys": ["a"], "doubled": 4, "up_outcome": "valid"}
    assert rows["group:b"] == {"seen_keys": ["b"], "doubled": 6, "up_outcome": "valid"}


def test_group_stage_lineage_rows_are_correct(cli, ws_root, demo, tmp_path):
    recipe = _recipe(
        tmp_path,
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n"
        "class G(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    n: int\n"
        "STAGES = [\n"
        "    Stage('g1', lambda ctx, g: G(n=len(g.items) + int(ctx.params['add'])), schema=G,\n"
        "          subject='group', text='default', params={'add': 0},\n"
        "          group_by=lambda i: i.locator[0]),\n"
        "    Stage('g2', lambda ctx, g: G(n=g.upstream['g1'][g.key].n + 1), schema=G,\n"
        "          subject='group', needs=('g1',), group_by=lambda i: i.locator[0]),\n"
        "    Stage('roll', lambda ctx, g: G(n=sum(v.n for v in g.upstream['g1'].values())),\n"
        "          schema=G, subject='run', needs=('g1',)),\n"
        "]\n",
    )
    assert stage(cli, ws_root, "g1", recipe=recipe)[0] == 0
    assert stage(cli, ws_root, "g2", recipe=recipe)[0] == 0
    assert stage(cli, ws_root, "roll", recipe=recipe)[0] == 0

    g1_ids = {
        r["subject_key"]: r["outcome_id"]
        for r in q(
            ws_root, "SELECT subject_key, outcome_id FROM current_records WHERE stage = 'r.g1'"
        )
    }
    g2_links = {
        r["subject_key"]: [
            u["upstream_outcome_id"]
            for u in q(
                ws_root,
                "SELECT oi.upstream_outcome_id FROM outcome_inputs oi"
                " JOIN outcomes u ON u.id = oi.upstream_outcome_id"
                " WHERE oi.outcome_id = ? AND u.stage = 'r.g1'",
                r["outcome_id"],
            )
        ]
        for r in q(
            ws_root, "SELECT subject_key, outcome_id FROM current_records WHERE stage = 'r.g2'"
        )
    }
    for key, g1_oid in g1_ids.items():
        assert g2_links[key] == [g1_oid]

    roll_oid = q(ws_root, "SELECT outcome_id FROM current_records WHERE stage = 'r.roll'")[0][
        "outcome_id"
    ]
    roll_links = {
        r["upstream_outcome_id"]
        for r in q(
            ws_root,
            "SELECT oi.upstream_outcome_id FROM outcome_inputs oi"
            " JOIN outcomes u ON u.id = oi.upstream_outcome_id"
            " WHERE oi.outcome_id = ? AND u.stage = 'r.g1'",
            roll_oid,
        )
    }
    assert roll_links == set(g1_ids.values())

    # Identical upstream rerun does not mark downstream group/run rows input_stale.
    assert stage(cli, ws_root, "g1", recipe=recipe)[0] == 0
    assert {
        r["stage"]: r["input_stale"]
        for r in q(
            ws_root,
            "SELECT stage, max(input_stale) AS input_stale FROM current_records"
            " WHERE stage IN ('r.g2', 'r.roll') GROUP BY stage",
        )
    } == {"r.g2": 0, "r.roll": 0}

    # Changing upstream group payloads marks both downstream group and run rows input_stale.
    assert stage(cli, ws_root, "g1", "--param", "add=10", recipe=recipe)[0] == 0
    assert {
        r["stage"]: r["input_stale"]
        for r in q(
            ws_root,
            "SELECT stage, max(input_stale) AS input_stale FROM current_records"
            " WHERE stage IN ('r.g2', 'r.roll') GROUP BY stage",
        )
    } == {"r.g2": 1, "r.roll": 1}


def test_item_stage_needing_group_stage_fails_at_startup(cli, ws_root, tmp_path):
    recipe = _recipe(
        tmp_path,
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n"
        "class M(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    x: int\n"
        "STAGES = [\n"
        "    Stage('grp', lambda ctx, g: M(x=1), schema=M, subject='group',\n"
        "          group_by=lambda i: i.locator),\n"
        "    Stage('itm', lambda ctx, i: M(x=1), schema=M, subject='item', needs=('grp',)),\n"
        "]\n",
    )
    res = cli("run", "--step", "itm", "--workspace", str(ws_root), "--recipe", str(recipe))
    assert res.exit_code == 2
    assert "stage itm: needs 'grp', which must be an earlier item stage" in res.stderr
    assert not ws_root.exists()


def test_group_stage_sees_excluded_items_when_group_by_uses_the_locator(cli, ws_root, tmp_path):
    """An exclusion is never silent: a group lists the items it had to leave out,
    and a group with no complete member still exists."""
    src = write_corpus(tmp_path / "src", {"a1": 1, "b1": -5})
    (src / "a2.txt").write_text("no parser for this type")  # no_text
    (src / "c1.txt").write_text("no parser for this type")  # a group with nothing complete
    prepare(cli, ws_root, src)
    recipe = _recipe(
        tmp_path,
        "import json\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Diagnostic, RecordEnvelope, Stage\n"
        "class E(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    name: str\n"
        "class G(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    count: int\n"
        "    excluded: list[str]\n"
        "def _ex(ctx, item):\n"
        "    d = json.loads(item.text)\n"
        "    if d['amount'] < 0:\n"
        "        return RecordEnvelope.quarantined('neg', Diagnostic(code='neg'))\n"
        "    return E(name=d['name'])\n"
        "def _grp(ctx, g):\n"
        "    return G(count=len(g.items),\n"
        "             excluded=sorted(f'{e.locator}:{e.reason}' for e in g.excluded))\n"
        "STAGES = [\n"
        "    Stage('extract', _ex, schema=E, text='default'),\n"
        "    Stage('grp', _grp, schema=G, subject='group', needs=('extract',),\n"
        "          text='default', group_by=lambda i: i.locator[0]),\n"
        "]\n",
    )
    assert stage(cli, ws_root, "extract", recipe=recipe)[0] == 0
    code, report = stage(cli, ws_root, "grp", recipe=recipe)
    assert code == 0, report
    payloads = {
        r["subject_key"]: json.loads(r["payload"])
        for r in q(
            ws_root, "SELECT subject_key, payload FROM current_records WHERE stage = 'r.grp'"
        )
    }
    assert payloads == {
        "group:a": {"count": 1, "excluded": ["a2.txt:no_text"]},
        "group:b": {"count": 0, "excluded": ["b1.json:upstream_not_valid"]},
        "group:c": {"count": 0, "excluded": ["c1.txt:no_text"]},
    }
    assert report["discovered"] == 3


def test_unhandled_subject_exception_and_all_quarantined_include_subject_and_reasons(
    cli, ws_root, tmp_path
):
    src = write_corpus(tmp_path / "src", {"ok": 1, "boom": 2})
    prepare(cli, ws_root, src)
    recipe = _recipe(
        tmp_path,
        "import json\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Diagnostic, RecordEnvelope, Stage\n"
        "class M(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    x: int\n"
        "def _crash(ctx, item):\n"
        "    if item.locator == 'boom.json':\n"
        "        raise ValueError('bad timestamp')\n"
        "    return M(x=1)\n"
        "def _quar(ctx, item):\n"
        "    return RecordEnvelope.quarantined(\n"
        "        'bad_schema_row',\n"
        "        Diagnostic(code='bad_schema_row', message=f'invalid {item.locator}'),\n"
        "    )\n"
        "STAGES = [\n"
        "    Stage('crash', _crash, schema=M, text='default'),\n"
        "    Stage('quar', _quar, schema=M, text='default'),\n"
        "]\n",
    )

    for workers in ("1", "2"):
        code, report = stage(cli, ws_root, "crash", "--workers", workers, "--fresh", recipe=recipe)
        assert code == 1
        assert "ValueError: bad timestamp" in report["error"]
        assert "boom.json" in report["error"]
        assert "subject=item:" in report["error"]

    code_q, report_q = stage(cli, ws_root, "quar", recipe=recipe)
    assert code_q == 1
    assert "AllQuarantined" in report_q["error"]
    assert "bad_schema_row=2" in report_q["error"]

    code_q1, report_q1 = stage(cli, ws_root, "quar", "--limit", "1", "--fresh", recipe=recipe)
    assert code_q1 == 1
    assert "bad_schema_row=1" in report_q1["error"]
    assert "invalid " in report_q1["error"]
