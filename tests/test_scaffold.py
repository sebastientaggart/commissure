"""`commissure init`: a solution folder from the built-in template.

Exercised through the real Typer app. The scaffolded recipe is loaded with `load_recipe`,
the production path, and must fail at startup for exactly one reason: no stages yet.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from commissure.recipes import RecipeError, load_recipe
from commissure.scaffold import TEMPLATE, TOKENS, ScaffoldError, scaffold_solution, template_files

EXPECTED = {
    "README.md",
    ".gitignore",
    "CHANGELOG.md",
    "release.toml",
    "docs/README.md",
    "docs/notes.md",
    "intake/README.md",
    "recipe/__init__.py",
    "recipe/AGENTS.md",
    "recipe/recipe.py",
    "recipe/schema.py",
    "recipe/extract.py",
    "recipe/analyze.py",
    "recipe/deliver.py",
}


def test_init_creates_the_documented_layout(cli, tmp_path):
    sol = tmp_path / "acme-monthly"
    result = cli("init", str(sol), "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["name"] == "acme-monthly"
    assert set(report["created"]) == EXPECTED
    for rel in EXPECTED:
        assert (sol / rel).is_file(), rel
    assert not (sol / "workspace").exists()  # data is created by the first run, never here

    # Tokens are substituted everywhere; the template's ignore file is written under its real name.
    for rel in EXPECTED:
        text = (sol / rel).read_text()
        for token in TOKENS:
            assert token not in text, (rel, token)
    assert "acme-monthly" in (sol / "README.md").read_text()
    assert "# Recipe: `acme-monthly`" in (sol / "recipe" / "AGENTS.md").read_text()
    assert not (sol / "_gitignore").exists()


def test_scaffolded_recipe_fails_at_startup_only_because_stages_is_empty(
    cli, tmp_path, monkeypatch
):
    sol = tmp_path / "sol"
    assert cli("init", str(sol)).exit_code == 0
    with pytest.raises(RecipeError, match="STAGES"):
        load_recipe(sol / "recipe")

    # Through the CLI from inside the folder: solution-dir defaults pick up recipe/,
    # the usage error is the same one, and the failure creates no workspace/.
    monkeypatch.delenv("COMMISSURE_WORKSPACE", raising=False)
    monkeypatch.delenv("COMMISSURE_RECIPE", raising=False)
    monkeypatch.chdir(sol)
    result = cli("run", "--step", "extract", "--json")
    assert result.exit_code == 2
    assert "STAGES" in result.stderr
    assert not (sol / "workspace").exists()


def test_init_accepts_an_existing_empty_folder_and_a_custom_name(cli, tmp_path):
    sol = tmp_path / "client-example"
    sol.mkdir()
    result = cli("init", str(sol), "--name", "example-ops", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["name"] == "example-ops"
    assert "example-ops" in (sol / "README.md").read_text()


def test_init_never_overwrites(cli, tmp_path):
    """The check can fail: a second init on the same folder is refused and changes nothing."""
    sol = tmp_path / "sol"
    assert cli("init", str(sol)).exit_code == 0
    readme = sol / "README.md"
    readme.write_text("operator edits\n")
    before = {p: os.stat(p).st_mtime_ns for p in sol.rglob("*") if p.is_file()}

    result = cli("init", str(sol))
    assert result.exit_code == 2
    assert "already has scaffold files" in result.stderr
    assert readme.read_text() == "operator edits\n"
    assert {p: os.stat(p).st_mtime_ns for p in sol.rglob("*") if p.is_file()} == before

    # One pre-existing scaffold file is enough to refuse, even in an otherwise empty folder,
    # and the refusal writes nothing.
    other = tmp_path / "other"
    (other / "recipe").mkdir(parents=True)
    (other / "recipe" / "schema.py").write_text("")
    result = cli("init", str(other))
    assert result.exit_code == 2
    assert not (other / "README.md").exists()


def test_init_rejects_a_file_path_and_a_bad_name(cli, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert cli("init", str(f)).exit_code == 2
    with pytest.raises(ScaffoldError, match="not a usable folder name"):
        scaffold_solution(tmp_path / "x", name="a/b")
    assert not (tmp_path / "x").exists()


def _git_add(repo: Path) -> set[str]:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    out = subprocess.check_output(["git", "ls-files"], cwd=repo, text=True)
    return set(out.splitlines())


def test_scaffold_gitignore_drops_client_data_and_keeps_the_intake_readme(cli, tmp_path):
    """The check can fail: a PDF under intake/ or a workspace file that gets added means
    the scaffold ignore stopped excluding client data."""
    sol = tmp_path / "sol"
    assert cli("init", str(sol)).exit_code == 0
    (sol / "intake" / "batch").mkdir()
    (sol / "intake" / "batch" / "a.pdf").write_bytes(b"pdf")
    (sol / "workspace").mkdir()
    (sol / "workspace" / "state.db").write_bytes(b"db")
    (sol / "workspace" / "state.db-wal").write_bytes(b"wal")
    (sol / "docs" / "plans").mkdir()
    (sol / "docs" / "plans" / "scratch.md").write_text("plan\n")
    (sol / "docs" / "plan-scratch.md").write_text("plan\n")

    tracked = _git_add(sol)
    assert "intake/README.md" in tracked
    assert "docs/README.md" in tracked
    assert "recipe/recipe.py" in tracked
    assert "intake/batch/a.pdf" not in tracked
    assert "workspace/state.db" not in tracked
    assert "workspace/state.db-wal" not in tracked
    assert "docs/plans/scratch.md" not in tracked
    assert "docs/plan-scratch.md" not in tracked


def test_core_ignore_keeps_only_gitkeep_under_solutions_and_opted_in_solution_tracks_corpus(
    tmp_path,
):
    """Core ignores solutions/* except solutions/.gitkeep (each solution is its own repo).
    A standalone solution repository that opts into tracking its corpus keeps workspace/state.db,
    blobs/, outputs/, and intake/ while SQLite side files and workspace.lock stay ignored."""
    root = Path(__file__).resolve().parents[1]
    core_repo = tmp_path / "core"
    core_repo.mkdir()
    (core_repo / ".gitignore").write_text((root / ".gitignore").read_text())
    core_samples = {
        "solutions/.gitkeep": b"",
        "solutions/.forbid.toml": b"forbid = []\n",
        "solutions/local-only/recipe/recipe.py": b"py",
        "solutions/local-only/intake/README.md": b"readme",
        "solutions/local-only/workspace/state.db": b"db",
        "docs/README.md": b"docs",
        "docs/plans/scratch.md": b"plan",
        "docs/plan-scratch.md": b"plan",
    }
    for rel, data in core_samples.items():
        path = core_repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    core_tracked = _git_add(core_repo)
    assert {"solutions/.gitkeep", "docs/README.md"} <= core_tracked
    assert {
        "solutions/.forbid.toml",
        "solutions/local-only/recipe/recipe.py",
        "solutions/local-only/intake/README.md",
        "solutions/local-only/workspace/state.db",
        "docs/plans/scratch.md",
        "docs/plan-scratch.md",
    }.isdisjoint(core_tracked)

    # Prove the check fails if solutions/* is removed from the core .gitignore.
    broken_core = tmp_path / "broken_core"
    broken_core.mkdir()
    (broken_core / ".gitignore").write_text(
        (root / ".gitignore").read_text().replace("solutions/*\n!solutions/.gitkeep\n", "")
    )
    (broken_core / "solutions" / "local-only" / "recipe").mkdir(parents=True)
    (broken_core / "solutions" / "local-only" / "recipe" / "recipe.py").write_bytes(b"py")
    assert "solutions/local-only/recipe/recipe.py" in _git_add(broken_core)

    sol_repo = tmp_path / "tracked-corpus"
    sol_repo.mkdir()
    scaffold_ignore = (TEMPLATE / "_gitignore").read_text()
    opted_in_ignore = "\n".join(
        line
        for line in scaffold_ignore.splitlines()
        if line not in {"workspace/", "state.db", "intake/*", "!intake/README.md"}
    )
    (sol_repo / ".gitignore").write_text(opted_in_ignore + "\n")
    sol_samples = {
        "workspace/state.db": b"db",
        "workspace/blobs/ab/file": b"blob",
        "workspace/outputs/dash.html": b"html",
        "workspace/state.db-wal": b"wal",
        "workspace/workspace.lock": b"lock",
        "intake/batch/a.pdf": b"pdf",
        "intake/README.md": b"readme",
        "tests/fixtures/sample.pdf": b"pdf",
        "recipe/recipe.py": b"py",
        ".DS_Store": b"ds",
        ".env": b"SECRET=1\n",
    }
    for rel, data in sol_samples.items():
        path = sol_repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    sol_tracked = _git_add(sol_repo)
    must = {
        "workspace/state.db",
        "workspace/blobs/ab/file",
        "workspace/outputs/dash.html",
        "intake/batch/a.pdf",
        "intake/README.md",
        "tests/fixtures/sample.pdf",
        "recipe/recipe.py",
    }
    must_not = {
        "workspace/state.db-wal",
        "workspace/workspace.lock",
        ".DS_Store",
        ".env",
    }
    assert must <= sol_tracked
    assert must_not.isdisjoint(sol_tracked)


def test_template_matches_what_init_writes():
    """The package template is the single source: every file in it is written, nothing else."""
    rel = {
        p.relative_to(TEMPLATE).as_posix().replace("_gitignore", ".gitignore")
        for p in template_files()
    }
    assert rel == EXPECTED
