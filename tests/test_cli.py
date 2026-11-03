import json
import shutil
import tomllib
from pathlib import Path

import commissure
from commissure.workspace import open_workspace

DEMO = Path(__file__).parent / "recipes" / "demo"


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert commissure.__version__ == expected


def test_help_lists_promised_commands(cli):
    assert cli("--help").exit_code == 0
    for command in ("probe", "run", "inspect", "recon", "search", "serve", "init"):
        assert cli(command, "--help").exit_code == 0, command


def test_inspect_without_a_workspace_is_a_usage_error(cli, tmp_path, monkeypatch):
    monkeypatch.delenv("COMMISSURE_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    result = cli("inspect")
    assert result.exit_code == 2
    assert "--workspace is required" in result.stderr
    assert not (tmp_path / "workspace").exists()


def test_unknown_step_without_a_recipe_is_a_usage_error(cli, tmp_path):
    # A real path, never a relative one: a step that ran would create it.
    result = cli("run", "--step", "analyze", "--workspace", str(tmp_path / "ws"))
    assert result.exit_code == 2
    assert "--recipe" in result.stderr
    assert not (tmp_path / "ws").exists()


def test_solution_dir_defaults_recipe_and_workspace(cli, tmp_path, monkeypatch):
    monkeypatch.delenv("COMMISSURE_WORKSPACE", raising=False)
    monkeypatch.delenv("COMMISSURE_RECIPE", raising=False)

    not_a_solution = tmp_path / "plain"
    not_a_solution.mkdir()
    monkeypatch.chdir(not_a_solution)
    assert cli("run", "--step", "extract").exit_code == 2

    sol = tmp_path / "solution"
    shutil.copytree(DEMO, sol / "recipe", ignore=shutil.ignore_patterns("__pycache__"))
    src = sol / "inputs"
    src.mkdir()
    (src / "alpha.json").write_text(json.dumps({"name": "alpha", "amount": 5}))
    monkeypatch.chdir(sol)

    # Usage errors fail before creating workspace/.
    assert cli("run", "--step", "ingest").exit_code == 2
    assert not (sol / "workspace").exists()

    # First valid run creates workspace/ automatically and records solution-dir.
    r_ingest = cli("run", "--step", "ingest", "--source", str(src), "--json")
    assert r_ingest.exit_code == 0, r_ingest.output
    assert (sol / "workspace" / "state.db").is_file()
    ingest_report = json.loads(r_ingest.stdout)
    r_parse = cli("run", "--step", "parse", "--json")
    assert r_parse.exit_code == 0, r_parse.output
    r_extract = cli("run", "--step", "extract", "--json")
    assert r_extract.exit_code == 0, r_extract.output
    report = json.loads(r_extract.stdout)
    assert Path(report["workspace"]) == (sol / "workspace").resolve()
    assert report["valid"] == 1

    ws = open_workspace(sol / "workspace")
    ingest_cfg = json.loads(
        ws.conn.execute(
            "SELECT config FROM runs WHERE id = ?", (ingest_report["run_id"],)
        ).fetchone()[0]
    )
    cfg = json.loads(
        ws.conn.execute("SELECT config FROM runs WHERE id = ?", (report["run_id"],)).fetchone()[0]
    )
    ws.close()
    expected_ws = {"value": str((sol / "workspace").resolve()), "from": "solution-dir"}
    assert ingest_cfg["workspace"] == expected_ws
    assert cfg["workspace"] == expected_ws
    assert cfg["recipe"] == {
        "value": str((sol / "recipe").resolve()),
        "from": "solution-dir",
    }
