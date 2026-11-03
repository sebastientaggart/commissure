"""The verdict log: declared checks, seeded samples, append-only verdicts, honest rates.

Driven through `commissure review` with the demo recipe's checks (`extract:/amount`,
`extract:/name`, `extract:/note` which matches nothing, `cite:/evidence/*`) and the core
parse checks over generated probe PDFs. Workspaces come from `open_workspace`.
"""

import json
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from commissure.recipes import load_recipe
from commissure.review import _sample_line, _spot_line, client_summary, wilson
from commissure.workspace import open_workspace

DEMO = Path(__file__).parent / "recipes" / "demo"
PROBE = Path(__file__).parent / "fixtures" / "probe_corpus"
NAMES = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
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


def review(cli, ws_root, *args, recipe=DEMO, reviewer="Pat Reviewer"):
    extra = ["--workspace", str(ws_root)]
    if recipe is not None and args[0] in ("checks", "sample", "next", "spot"):
        extra += ["--recipe", str(recipe)]
    if reviewer is not None and args[0] in ("sample", "record"):
        extra += ["--reviewer", reviewer]
    return cli("review", *args, *extra, "--json")


def ok(res):
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def q(ws_root, sql, *params):
    ws = open_workspace(ws_root)
    try:
        return [dict(r) for r in ws.conn.execute(sql, params)]
    finally:
        ws.close()


@pytest.fixture
def extracted(cli, ws_root, tmp_path):
    """Six JSON items through parse and the demo `extract` stage."""
    src = tmp_path / "src"
    src.mkdir()
    for i, name in enumerate(NAMES, start=1):
        (src / f"{name}.json").write_text(json.dumps({"name": name, "amount": i * 10}))
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    res = cli("run", "--step", "extract", "--workspace", str(ws_root), "--recipe", str(DEMO))
    assert res.exit_code == 0, res.output
    return src


def test_checks_stay_out_of_the_stage_fingerprint(tmp_path):
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    before = load_recipe(recipe).stage_fps
    recipe_py = recipe / "recipe.py"
    recipe_py.write_text(
        recipe_py.read_text()
        .replace("Is this the amount the item states?", "Does the item state this amount?")
        .replace(
            '            Check("/name", "Is this the item\'s name?"),\n',
            '            Check("/name", "Is this the item\'s name?"),\n'
            '            Check("/*", "A new question about every field."),\n',
        )
    )
    assert "Does the item state this amount?" in recipe_py.read_text()
    assert load_recipe(recipe).stage_fps == before
    # Control: a change to the stage's code does move its fingerprint.
    (recipe / "extract.py").write_text((recipe / "extract.py").read_text() + "\n# changed\n")
    assert load_recipe(recipe).stage_fps["extract"] != before["extract"]


def test_a_seeded_sample_is_reproducible_and_records_how_it_was_drawn(cli, ws_root, extracted):
    first = ok(
        review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "3", "--seed", "7")
    )
    second = ok(
        review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "3", "--seed", "7")
    )
    assert (first["population"], first["n"], first["seed"], first["census"]) == (6, 3, 7, False)

    def drawn(sample_id):
        return [
            (r["subject_key"], r["pointer"])
            for r in q(
                ws_root,
                "SELECT subject_key, pointer FROM review_instances WHERE sample_id = ?"
                " ORDER BY position",
                sample_id,
            )
        ]

    assert drawn(first["sample_id"]) == drawn(second["sample_id"])
    [row] = q(ws_root, "SELECT * FROM review_samples WHERE id = ?", first["sample_id"])
    assert (row["seed"], row["population"], row["n"], row["reviewer"]) == (7, 6, 3, "Pat Reviewer")
    assert row["question"] == "Is this the amount the item states?"

    unseeded = ok(review(cli, ws_root, "sample", "--check", "extract:/name", "--n", "10"))
    assert unseeded["census"] and unseeded["n"] == 6 and isinstance(unseeded["seed"], int)


def test_patterns_select_scalars_and_list_elements_and_nothing_is_an_error(cli, ws_root, extracted):
    checks = {c["id"]: c for c in ok(review(cli, ws_root, "checks"))["checks"]}
    assert checks["extract:/amount"]["population"] == 6
    assert checks["extract:/note"]["population"] == 0
    res = cli(
        "run", "--step", "cite", "--workspace", str(ws_root), "--recipe", str(DEMO),
        "--param", "needle=a",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    checks = {c["id"]: c for c in ok(review(cli, ws_root, "checks"))["checks"]}
    assert checks["cite:/evidence/*"]["population"] == len(NAMES)  # one pointer per record
    sampled = ok(review(cli, ws_root, "sample", "--check", "cite:/evidence/*", "--n", "1"))
    shown = ok(review(cli, ws_root, "next", "--sample", str(sampled["sample_id"])))["instance"]
    assert len(shown["evidence"]) == 1 and shown["evidence"][0]["span_text"] == "a"
    nothing = review(cli, ws_root, "sample", "--check", "extract:/note", "--n", "2")
    assert nothing.exit_code == 2 and "matches no instances" in nothing.output


def test_sampling_refuses_an_unfinished_snapshot_and_stale_records(cli, ws_root, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in NAMES:
        (src / f"{name}.json").write_text(json.dumps({"name": name, "amount": 5}))
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    run = ["run", "--step", "extract", "--workspace", str(ws_root), "--recipe", str(recipe)]
    assert cli(*run, "--limit", "2").exit_code == 0

    unfinished = review(
        cli, ws_root, "sample", "--check", "extract:/amount", "--n", "2", recipe=recipe
    )
    assert unfinished.exit_code == 2 and "did not finish its scope" in unfinished.output

    assert cli(*run).exit_code == 0
    ok(review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "2", recipe=recipe))
    (recipe / "extract.py").write_text((recipe / "extract.py").read_text() + "\n# changed\n")
    stale = review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "2", recipe=recipe)
    assert stale.exit_code == 2 and "6 of 6 current records of extract are stale" in stale.output


def test_verdicts_are_append_only_and_the_latest_counts(cli, ws_root, extracted):
    sample = ok(
        review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "2", "--seed", "1")
    )
    first = ok(review(cli, ws_root, "next"))["instance"]["instance_id"]
    ok(review(cli, ws_root, "record", str(first), "wrong", "--note", "off by ten"))
    ok(review(cli, ws_root, "record", str(first), "correct", "--note", "misread it"))
    [s] = ok(review(cli, ws_root, "report"))["samples"]
    assert (s["id"], s["correct"], s["wrong"], s["pending"]) == (sample["sample_id"], 1, 0, 1)
    ws = open_workspace(ws_root)
    try:
        for sql in (
            "UPDATE verdicts SET answer = 'wrong'",
            "DELETE FROM verdicts",
            "UPDATE review_samples SET seed = 2",
            "DELETE FROM review_instances",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                ws.conn.execute(sql)
    finally:
        ws.close()


def test_spot_checks_are_reported_but_never_rated(cli, ws_root, extracted):
    ok(review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "6"))
    spot = ok(
        review(
            cli, ws_root, "spot", "--check", "extract:/amount", "--subject", "alpha.json",
            "--pointer", "/amount",
        )
    )["instance"]  # fmt: skip
    assert spot["sample_id"] is None and spot["value"] == 10
    ok(review(cli, ws_root, "record", str(spot["instance_id"]), "wrong"))
    rep = ok(review(cli, ws_root, "report"))
    [s] = rep["samples"]
    assert (s["wrong"], s["pending"], s["rate"]) == (0, 6, None)
    assert rep["spot_checks"] == [
        {"check_id": "extract:/amount", "correct": 0, "wrong": 1, "unclear": 0}
    ]
    # A spot check is never handed out as pending sample work, even one with no verdict.
    ok(
        review(
            cli, ws_root, "spot", "--check", "extract:/amount", "--subject", "bravo.json",
            "--pointer", "/amount",
        )
    )  # fmt: skip
    assert ok(review(cli, ws_root, "next"))["instance"]["sample_id"] == s["id"]


def test_wilson_bounds_match_known_values():
    low, high = wilson(0, 30)
    assert low == 0 and high == pytest.approx(0.1135, abs=1e-4)
    low, high = wilson(1, 40)
    assert (round(low, 4), round(high, 4)) == (0.0044, 0.1288)
    assert wilson(0, 0) is None


def test_rates_use_correct_and_wrong_only(cli, ws_root, extracted):
    ok(review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "6", "--seed", "3"))
    for answer in ("wrong", "correct", "correct", "unclear"):
        instance = ok(review(cli, ws_root, "next"))["instance"]["instance_id"]
        ok(review(cli, ws_root, "record", str(instance), answer))
    [s] = ok(review(cli, ws_root, "report"))["samples"]
    assert (s["correct"], s["wrong"], s["unclear"], s["pending"]) == (2, 1, 1, 2)
    assert s["rate"] == pytest.approx(1 / 3)
    assert s["interval_95"] == pytest.approx(list(wilson(1, 3)))


def test_writes_without_a_reviewer_are_refused(cli, ws_root, extracted, monkeypatch):
    monkeypatch.delenv("COMMISSURE_REVIEWER", raising=False)
    res = review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "2", reviewer=None)
    assert res.exit_code == 2 and "a reviewer is required" in res.output
    ok(review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "2"))
    instance = ok(review(cli, ws_root, "next"))["instance"]["instance_id"]
    res = review(cli, ws_root, "record", str(instance), "correct", reviewer=None)
    assert res.exit_code == 2 and "a reviewer is required" in res.output
    assert q(ws_root, "SELECT count(*) AS n FROM verdicts")[0]["n"] == 0


def test_a_verdict_is_recorded_while_another_process_holds_the_lock(cli, ws_root, extracted):
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(ws_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        blocked = cli("run", "--step", "parse", "--workspace", str(ws_root))
        assert blocked.exit_code == 3  # the lock is real
        ok(review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "1"))
        instance = ok(review(cli, ws_root, "next"))["instance"]["instance_id"]
        ok(review(cli, ws_root, "record", str(instance), "correct"))
    finally:
        holder.communicate("\n", timeout=10)
    [s] = ok(review(cli, ws_root, "report"))["samples"]
    assert s["correct"] == 1


def test_an_earlier_verdict_on_an_identical_instance_is_shown_and_not_counted(
    cli, ws_root, extracted
):
    ok(review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "6", "--seed", "5"))
    first = ok(review(cli, ws_root, "next"))["instance"]
    ok(review(cli, ws_root, "record", str(first["instance_id"]), "wrong", "--note", "says 15"))
    later = ok(
        review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "6", "--seed", "5")
    )
    shown = ok(review(cli, ws_root, "next", "--sample", str(later["sample_id"])))["instance"]
    assert (shown["subject_key"], shown["pointer"]) == (first["subject_key"], first["pointer"])
    [earlier] = shown["earlier_verdicts"]
    assert (earlier["answer"], earlier["note"], earlier["instance_id"]) == (
        "wrong",
        "says 15",
        first["instance_id"],
    )
    by_id = {s["id"]: s for s in ok(review(cli, ws_root, "report"))["samples"]}
    assert (by_id[later["sample_id"]]["wrong"], by_id[later["sample_id"]]["pending"]) == (0, 6)
    human = cli(
        "review", "next", "--sample", str(later["sample_id"]), "--workspace", str(ws_root),
        "--recipe", str(DEMO),
    )  # fmt: skip
    assert "[shown only; it does not count here]" in human.stdout


def test_unknown_checks_samples_and_instances_are_usage_errors(cli, ws_root, extracted):
    assert review(cli, ws_root, "sample", "--check", "extract:/nope", "--n", "1").exit_code == 2
    no_recipe = review(
        cli, ws_root, "sample", "--check", "extract:/amount", "--n", "1", recipe=None
    )
    assert no_recipe.exit_code == 2 and "recipe checks need --recipe" in no_recipe.output
    assert review(cli, ws_root, "record", "999", "correct").exit_code == 2
    assert review(cli, ws_root, "next", "--sample", "999").exit_code == 2
    ok(review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "1"))
    instance = ok(review(cli, ws_root, "next"))["instance"]["instance_id"]
    bad = review(cli, ws_root, "record", str(instance), "maybe")
    assert bad.exit_code == 2 and "answer must be one of" in bad.output


def test_parse_checks_sample_page_findings_without_a_recipe(cli, ws_root, tmp_path):
    """scanned.pdf has two image-only pages; mixed.pdf has unreadable pages 2, 3, 6."""
    src = tmp_path / "src"
    src.mkdir()
    for name in ("scanned.pdf", "mixed.pdf"):
        shutil.copy(PROBE / name, src / name)
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    checks = {c["id"]: c for c in ok(review(cli, ws_root, "checks", recipe=None))["checks"]}
    assert checks["parse:unreadable_pages"]["population"] == 5
    ok(review(cli, ws_root, "sample", "--check", "parse:unreadable_pages", "--n", "5", recipe=None))
    shown = ok(review(cli, ws_root, "next", recipe=None))["instance"]
    assert shown["pointer"] is None and shown["value"] == {"page": shown["page"]}
    assert shown["question"].startswith("Is this page unreadable")
    [original] = shown["originals"]
    [page] = original["pages"]
    assert page["url"].endswith(f"#page={shown['page']}")
    assert original["original_path"].endswith(shown["subject_key"] and original["locator"])


# --- client QA summary (reads_review / ctx.review_report) -----------------------------


def _recipe_with_qa(tmp_path: Path, *, reads_review: bool, call_report: bool) -> Path:
    """A demo copy with a run stage that optionally reads the review snapshot.

    The folder stays named `demo` so its stage prefix matches the `extracted` fixture's
    `demo.extract` outcomes. The checked-in demo's STAGES are not edited.
    """
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    schema = recipe / "schema.py"
    schema.write_text(
        schema.read_text()
        + "\n\nclass QaSummary(BaseModel):\n"
        + "    model_config = ConfigDict(extra='forbid')\n"
        + "    lines: list[str]\n"
    )
    summarize = recipe / "summarize.py"
    summarize.write_text(
        summarize.read_text()
        + "\nfrom .schema import QaSummary\n\n"
        + "def qa_summary(ctx, group):\n"
        + (
            "    return QaSummary(lines=ctx.review_report()['lines'])\n"
            if call_report
            else "    return QaSummary(lines=[])\n"
        )
    )
    flag = ", reads_review=True" if reads_review else ""
    recipe_py = recipe / "recipe.py"
    recipe_py.write_text(
        recipe_py.read_text()
        .replace(
            "from .schema import Carried, Cited, Entry, Score, Total",
            "from .schema import Carried, Cited, Entry, QaSummary, Score, Total",
        )
        .replace(
            "from .summarize import by_initial, require_no_quarantine, total",
            "from .summarize import by_initial, qa_summary, require_no_quarantine, total",
        )
        .replace(
            'Stage("total", total, schema=Total, subject="run", needs=("extract",)),',
            'Stage("total", total, schema=Total, subject="run", needs=("extract",)),\n'
            f'    Stage("qa", qa_summary, schema=QaSummary, subject="run",'
            f' needs=("extract",), allow_empty=True{flag}),',
        )
    )
    return recipe


def test_default_reads_review_stays_out_of_the_stage_fingerprint(tmp_path):
    recipe = tmp_path / "demo"
    shutil.copytree(DEMO, recipe, ignore=shutil.ignore_patterns("__pycache__"))
    before = load_recipe(recipe).stage_fps
    # Adding the field at its default must not move any fingerprint (same omission as checks).
    recipe_py = recipe / "recipe.py"
    recipe_py.write_text(
        recipe_py.read_text().replace(
            'Stage("total", total, schema=Total, subject="run", needs=("extract",)),',
            'Stage("total", total, schema=Total, subject="run", needs=("extract",),'
            " reads_review=False),",
        )
    )
    assert load_recipe(recipe).stage_fps == before
    recipe_py.write_text(recipe_py.read_text().replace("reads_review=False", "reads_review=True"))
    after = load_recipe(recipe).stage_fps
    assert after["total"] != before["total"]
    assert {k: after[k] for k in after if k != "total"} == {
        k: before[k] for k in before if k != "total"
    }


def test_review_report_without_reads_review_fails_the_run(cli, ws_root, extracted, tmp_path):
    recipe = _recipe_with_qa(tmp_path, reads_review=False, call_report=True)
    res = cli("run", "--step", "qa", "--workspace", str(ws_root), "--recipe", str(recipe))
    assert res.exit_code == 1
    assert "reads_review=True" in res.output


def test_reads_review_payload_and_verdict_staleness(cli, ws_root, extracted, tmp_path):
    recipe = _recipe_with_qa(tmp_path, reads_review=True, call_report=True)
    # Empty review: no lines, no invented rate.
    res = cli("run", "--step", "qa", "--workspace", str(ws_root), "--recipe", str(recipe), "--json")
    assert res.exit_code == 0, res.output
    [empty] = q(
        ws_root,
        "SELECT o.input_fp, r.payload FROM current_outcomes o"
        " JOIN records r ON r.id = o.record_id WHERE o.stage = 'demo.qa'",
    )
    assert json.loads(empty["payload"])["lines"] == []
    empty_fp = empty["input_fp"]

    # Drawing the first sample changes the input fingerprint of a reads_review stage.
    ok(review(cli, ws_root, "sample", "--check", "extract:/amount", "--n", "6", "--seed", "3"))
    res = cli("run", "--step", "qa", "--workspace", str(ws_root), "--recipe", str(recipe), "--json")
    assert res.exit_code == 0, res.output
    [drawn] = q(
        ws_root,
        "SELECT o.input_fp, o.run_id, r.payload FROM current_outcomes o"
        " JOIN records r ON r.id = o.record_id WHERE o.stage = 'demo.qa'",
    )
    assert drawn["input_fp"] != empty_fp
    [line] = json.loads(drawn["payload"])["lines"]
    assert "all 6 instances of extract:/amount" in line
    assert "none judged" in line
    assert "drawn by Pat Reviewer" in line
    drawn_run = drawn["run_id"]
    drawn_fp = drawn["input_fp"]

    # A stage without the flag keeps the same input_fp across a verdict.
    def run_total():
        return cli(
            "run", "--step", "total", "--workspace", str(ws_root), "--recipe", str(recipe)
        )  # fmt: skip

    assert run_total().exit_code == 0
    [total_before] = q(ws_root, "SELECT input_fp FROM current_outcomes WHERE stage = 'demo.total'")
    for answer in ("wrong", "correct", "correct", "unclear"):
        instance = ok(review(cli, ws_root, "next"))["instance"]["instance_id"]
        ok(review(cli, ws_root, "record", str(instance), answer))
    assert run_total().exit_code == 0
    [total_after] = q(ws_root, "SELECT input_fp FROM current_outcomes WHERE stage = 'demo.total'")
    assert total_after["input_fp"] == total_before["input_fp"]

    # The reads_review stage sees the verdict and writes a new outcome.
    res = cli("run", "--step", "qa", "--workspace", str(ws_root), "--recipe", str(recipe), "--json")
    assert res.exit_code == 0, res.output
    [judged] = q(
        ws_root,
        "SELECT o.input_fp, o.run_id, r.payload FROM current_outcomes o"
        " JOIN records r ON r.id = o.record_id WHERE o.stage = 'demo.qa'",
    )
    assert judged["input_fp"] != drawn_fp
    assert judged["run_id"] != drawn_run
    [line] = json.loads(judged["payload"])["lines"]
    assert "1 wrong of 3 (33.3%" in line
    assert "1 unclear" in line
    assert "2 pending" in line
    assert "verdicts by Pat Reviewer" in line


def test_client_summary_line_wording_and_empty_review(cli, ws_root, extracted):
    ws = open_workspace(ws_root)
    try:
        empty = client_summary(ws.conn)
    finally:
        ws.close()
    assert empty == {"samples": [], "spot_checks": [], "lines": []}

    # Zero wrong of 30: the published upper bound matches Wilson, not a rounded guess.
    lo, hi = wilson(0, 30)
    line = _sample_line(
        {
            "id": 1,
            "check_id": "extract:/amount",
            "question": "Is this the amount the item states?",
            "snapshot_run_id": 9,
            "seed": 1,
            "population": 30,
            "n": 30,
            "reviewer": "Pat Reviewer",
            "created_at": "2026-11-02T00:00:00.000Z",
            "correct": 30,
            "wrong": 0,
            "unclear": 0,
            "pending": 0,
            "rate": 0.0,
            "interval_95": [lo, hi],
            "reviewers": ["Pat Reviewer"],
            "reviewed_at": "2026-11-02T01:00:00.000Z",
        }
    )
    assert f"95% CI {lo:.1%} to {hi:.1%}" in line
    assert "0 wrong of 30 (0.0%" in line
    assert "all 30 instances of extract:/amount" in line
    assert "sampled at random" not in line

    # Spot checks are counted and labelled, never rated.
    spot = _spot_line({"check_id": "extract:/amount", "correct": 0, "wrong": 1, "unclear": 0})
    assert "not rated" in spot
    assert "%" not in spot

    # A partial draw says sampled; a census does not (covered above).
    partial = _sample_line(
        {
            "check_id": "extract:/name",
            "question": "Is this the item's name?",
            "snapshot_run_id": 2,
            "seed": 7,
            "population": 6,
            "n": 3,
            "reviewer": "Pat Reviewer",
            "created_at": "2026-11-02T00:00:00.000Z",
            "correct": 0,
            "wrong": 0,
            "unclear": 0,
            "pending": 3,
            "rate": None,
            "interval_95": None,
            "reviewers": [],
            "reviewed_at": None,
        }
    )
    assert "3 of 6 instances of extract:/name sampled at random" in partial
    assert "none judged" in partial
    assert "3 pending" in partial
    assert "verdicts by" not in partial
