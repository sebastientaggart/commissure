"""The example solution (`commissure new --example`), end to end against this checkout.

What a developer's first five minutes run: scaffold, ingest, parse, both stages, the
interface, and a review sample. The example's own tests run here too, so a core change that
breaks them fails in core first. A broken evidence span must fail the run: the example's
point is that every result is checked against its original.
"""

import json
import subprocess
import sys
import threading
import urllib.request
from contextlib import contextmanager

import pytest

from commissure.serve import InterfaceApp, make_server

STEPS = [
    ("ingest", "--source", "intake/comments", "--role", "comment"),
    ("parse",),
    ("extract",),
    ("tally",),
]


@pytest.fixture
def example(cli, tmp_path, monkeypatch):
    sol = tmp_path / "example"
    assert cli("new", str(sol), "--example").exit_code == 0
    monkeypatch.delenv("COMMISSURE_WORKSPACE", raising=False)
    monkeypatch.delenv("COMMISSURE_RECIPE", raising=False)
    monkeypatch.chdir(sol)  # solution-dir defaults: recipe/ and workspace/, as a person runs it
    return sol


def _run_all(cli) -> dict[str, dict]:
    reports = {}
    for step, *rest in STEPS:
        result = cli("run", "--step", step, *rest, "--json")
        reports[step] = {"exit": result.exit_code, **json.loads(result.stdout or "{}")}
        if result.exit_code:
            break
    return reports


@contextmanager
def _serving(app: InterfaceApp):
    server = make_server(app, host="127.0.0.1", port=0)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.wait_for_idle(timeout=5)


def test_example_runs_offline_and_serves_its_results(cli, example):
    reports = _run_all(cli)
    assert [r["exit"] for r in reports.values()] == [0, 0, 0, 0]
    ex = reports["extract"]
    assert (ex["valid"], ex["warning"], ex["quarantined"], ex["skipped"]) == (20, 1, 1, 1)

    with _serving(InterfaceApp(example / "workspace", example / "recipe")) as url:
        page = urllib.request.urlopen(url + "/").read().decode()
    assert "Proposal 7 comments" in page
    assert "I am in favor" in page  # a quote, not just a count
    assert "c23-scan.pdf" in page  # what was set aside is named
    assert "//cdn" not in page and "https://" not in page.split("<body", 1)[1]

    checks = json.loads(cli("review", "checks", "--json").stdout)["checks"]
    ids = {c["id"] for c in checks}
    assert {"extract:/position_evidence", "extract:/topics/*/evidence"} <= ids
    sample = cli(
        "review",
        "sample",
        "--check",
        "extract:/position_evidence",
        "--n",
        "5",
        "--reviewer",
        "tester",
        "--json",
    )
    assert sample.exit_code == 0, sample.output


def test_example_shows_an_empty_state_before_any_run(example):
    with _serving(InterfaceApp(example / "workspace", example / "recipe")) as url:
        page = urllib.request.urlopen(url + "/").read().decode()
    assert "No results yet" in page


def test_a_broken_evidence_span_fails_the_run(cli, example):
    """The check can fail: an off-by-one quote is a recipe bug, and core refuses the record."""
    extract = example / "recipe" / "extract.py"
    text = extract.read_text()
    broken = text.replace(
        'Evidence.span(item, *first.span(), kind="position_phrase")',
        'Evidence.span(item, *first.span(), kind="position_phrase").model_copy('
        'update={"text": first.group(0).upper()})',
    )
    assert broken != text
    extract.write_text(broken)
    reports = _run_all(cli)
    assert reports["extract"]["exit"] == 1
    assert "evidence" in (reports["extract"]["error"] or "").lower(), reports["extract"]


def test_the_examples_own_tests_pass_against_this_checkout(example):
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist"],
        cwd=example,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-2000:]
    assert "3 passed" in out.stdout
