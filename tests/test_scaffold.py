"""`commissure new` and `commissure agent-kit`: a solution project from the built-in template.

Exercised through the real Typer app. The scaffolded recipe is loaded with `load_recipe`,
the production path, and must fail at startup for exactly one reason: no stages yet. The
opt-in integration test builds the wheel and runs the new project's own `uv sync`, tests, and
lint against it, which is how the templates are linted at all (core's ruff skips them).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from commissure.recipes import RecipeError, load_recipe
from commissure.scaffold import (
    KIT,
    KIT_DIR,
    SKILL_LINK,
    SKILLS_DIR,
    TEMPLATES,
    TOKENS,
    ScaffoldError,
    kit_skills,
    requirement,
    scaffold_solution,
    template_files,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = TEMPLATES["project"]
EXPECTED = {
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    ".gitignore",
    "pyproject.toml",
    "CHANGELOG.md",
    ".claude/settings.json",
    "docs/README.md",
    "docs/notes.md",
    "intake/README.md",
    "recipe/__init__.py",
    "recipe/recipe.py",
    "recipe/schema.py",
    "recipe/extract.py",
    "tests/test_recipe.py",
}
SKILLS = {"build-recipe-stage", "investigate-solution"}


def kit_expected() -> set[str]:
    files = {
        (KIT_DIR / p.relative_to(KIT)).as_posix()
        for p in KIT.rglob("*")
        if p.is_file() and p.name != ".DS_Store"
    }
    return files | {(KIT_DIR / "VERSION").as_posix()} | {f".claude/skills/{s}" for s in SKILLS}


def test_new_creates_the_documented_layout(cli, tmp_path):
    sol = tmp_path / "acme-monthly"
    result = cli("new", str(sol), "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["name"] == "acme-monthly"
    assert set(report["created"]) == EXPECTED | kit_expected()
    for rel in EXPECTED:
        assert (sol / rel).is_file(), rel
    assert not (sol / "workspace").exists()  # data is created by the first run, never here

    # Tokens are substituted everywhere; `_`-prefixed template names are written as their real
    # names, so the template's own ignore rules, project file, and agent files never apply here.
    for rel in EXPECTED:
        text = (sol / rel).read_text()
        for token in TOKENS:
            assert token not in text, (rel, token)
    assert not [p for p in sol.rglob("_*") if p.name != "__init__.py"]
    assert 'name = "acme-monthly"' in (sol / "pyproject.toml").read_text()
    assert f'dependencies = ["{requirement()}"]' in (sol / "pyproject.toml").read_text()
    assert (sol / "AGENTS.md").read_text().startswith("# acme-monthly\n")
    assert (sol / "CLAUDE.md").read_text().split() == [
        "@AGENTS.md",
        "@.agents/commissure/AGENTS.md",
    ]


def test_requirement_allows_patch_upgrades_only(monkeypatch):
    """`~=X.Y.Z`: a 0.x minor release may break recipes, so the pin stops before it."""
    import commissure.scaffold as scaffold

    monkeypatch.setattr(scaffold, "version", lambda _: "0.4.2")
    assert scaffold.requirement() == "commissure~=0.4.2"
    monkeypatch.setattr(scaffold, "version", lambda _: "0.5.0.dev3")
    assert scaffold.requirement() == "commissure~=0.5.0"


def test_scaffolded_recipe_fails_at_startup_only_because_stages_is_empty(
    cli, tmp_path, monkeypatch
):
    sol = tmp_path / "sol"
    assert cli("new", str(sol)).exit_code == 0
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


def test_new_accepts_an_existing_folder_and_a_custom_name(cli, tmp_path):
    sol = tmp_path / "client-example"
    (sol / "intake" / "batch").mkdir(parents=True)
    (sol / "intake" / "batch" / "a.pdf").write_bytes(b"pdf")
    result = cli("new", str(sol), "--name", "example-ops", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["name"] == "example-ops"
    assert 'name = "example-ops"' in (sol / "pyproject.toml").read_text()
    assert (sol / "intake" / "batch" / "a.pdf").read_bytes() == b"pdf"


def test_new_never_overwrites(cli, tmp_path):
    """The check can fail: a second `new` on the same folder is refused and changes nothing."""
    sol = tmp_path / "sol"
    assert cli("new", str(sol)).exit_code == 0
    readme = sol / "README.md"
    readme.write_text("operator edits\n")
    before = {p: os.stat(p).st_mtime_ns for p in sol.rglob("*") if p.is_file()}

    result = cli("new", str(sol))
    assert result.exit_code == 2
    assert "already has scaffold files" in result.stderr
    assert readme.read_text() == "operator edits\n"
    assert {p: os.stat(p).st_mtime_ns for p in sol.rglob("*") if p.is_file()} == before

    # One pre-existing scaffold or kit file is enough to refuse, and the refusal writes nothing.
    for clash in ("recipe/schema.py", ".agents/commissure/AGENTS.md"):
        other = tmp_path / clash.replace("/", "_")
        (other / clash).parent.mkdir(parents=True)
        (other / clash).write_text("")
        result = cli("new", str(other))
        assert result.exit_code == 2, clash
        assert not (other / "README.md").exists()
        assert not (other / ".claude").exists()


@pytest.mark.parametrize("bad", ["a/b", "-lead", "trail-", "has space", "."])
def test_new_rejects_a_bad_name(tmp_path, bad):
    with pytest.raises(ScaffoldError, match="must be letters"):
        scaffold_solution(tmp_path / "x", name=bad)
    assert not (tmp_path / "x").exists()


def test_new_rejects_a_file_path(cli, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert cli("new", str(f)).exit_code == 2


def _git_add(repo: Path) -> set[str]:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    out = subprocess.check_output(["git", "ls-files"], cwd=repo, text=True)
    return set(out.splitlines())


def test_scaffold_gitignore_drops_client_data_and_keeps_the_agent_kit(cli, tmp_path):
    """The check can fail: a PDF under intake/, a workspace file, or local agent settings that
    get added mean the scaffold ignore stopped excluding them."""
    sol = tmp_path / "sol"
    assert cli("new", str(sol)).exit_code == 0
    (sol / "intake" / "batch").mkdir()
    (sol / "intake" / "batch" / "a.pdf").write_bytes(b"pdf")
    (sol / "workspace").mkdir()
    (sol / "workspace" / "state.db").write_bytes(b"db")
    (sol / "workspace" / "state.db-wal").write_bytes(b"wal")
    (sol / "docs" / "plans").mkdir()
    (sol / "docs" / "plans" / "scratch.md").write_text("plan\n")
    (sol / "docs" / "plan-scratch.md").write_text("plan\n")
    (sol / ".claude" / "settings.local.json").write_text("{}\n")
    (sol / "CLAUDE.local.md").write_text("machine notes\n")

    tracked = _git_add(sol)
    assert {
        "intake/README.md",
        "docs/README.md",
        "recipe/recipe.py",
        ".claude/settings.json",
        ".agents/commissure/AGENTS.md",
        ".claude/skills/build-recipe-stage",
    } <= tracked
    assert {
        "intake/batch/a.pdf",
        "workspace/state.db",
        "workspace/state.db-wal",
        "docs/plans/scratch.md",
        "docs/plan-scratch.md",
        ".claude/settings.local.json",
        "CLAUDE.local.md",
    }.isdisjoint(tracked)


def test_opted_in_solution_tracks_its_corpus_but_never_side_files(tmp_path):
    """A solution that drops the workspace and intake lines keeps state.db, blobs/, outputs/,
    and intake/ while SQLite side files, the lock, and secrets stay ignored."""
    sol_repo = tmp_path / "tracked-corpus"
    sol_repo.mkdir()
    scaffold_ignore = (TEMPLATE / "_gitignore").read_text()
    opted_in_ignore = "\n".join(
        line
        for line in scaffold_ignore.splitlines()
        if line not in {"workspace/", "state.db", "intake/*", "!intake/README.md"}
    )
    (sol_repo / ".gitignore").write_text(opted_in_ignore + "\n")
    samples = {
        "workspace/state.db": b"db",
        "workspace/blobs/ab/file": b"blob",
        "workspace/outputs/dash.html": b"html",
        "workspace/state.db-wal": b"wal",
        "workspace/workspace.lock": b"lock",
        "intake/batch/a.pdf": b"pdf",
        "intake/README.md": b"readme",
        "recipe/recipe.py": b"py",
        ".DS_Store": b"ds",
        ".env": b"SECRET=1\n",
    }
    for rel, data in samples.items():
        path = sol_repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    tracked = _git_add(sol_repo)
    assert {
        "workspace/state.db",
        "workspace/blobs/ab/file",
        "workspace/outputs/dash.html",
        "intake/batch/a.pdf",
        "intake/README.md",
        "recipe/recipe.py",
    } <= tracked
    assert {
        "workspace/state.db-wal",
        "workspace/workspace.lock",
        ".DS_Store",
        ".env",
    }.isdisjoint(tracked)


def test_agent_kit_replaces_only_the_kit(cli, tmp_path, monkeypatch):
    sol = tmp_path / "sol"
    assert cli("new", str(sol)).exit_code == 0
    kit = sol / KIT_DIR
    # A stale kit: an edited file, a file the installed kit no longer has, a dropped skill's link.
    (kit / "AGENTS.md").write_text("edited\n")
    (kit / "old.md").write_text("gone in this version\n")
    (sol / SKILLS_DIR / "retired-skill").symlink_to(SKILL_LINK + "retired-skill")
    # The solution's own skills (a folder, and a link elsewhere) and files stay as they are.
    own = sol / SKILLS_DIR / "own-skill"
    own.mkdir()
    (own / "SKILL.md").write_text("mine\n")
    (sol / SKILLS_DIR / "shared").symlink_to("../../somewhere/else")
    (sol / "AGENTS.md").write_text("contract\n")

    monkeypatch.chdir(sol)
    result = cli("agent-kit", "--json")
    assert result.exit_code == 0, result.output
    assert (kit / "AGENTS.md").read_bytes() == (KIT / "AGENTS.md").read_bytes()
    assert not (kit / "old.md").exists()
    assert (kit / "VERSION").read_text().strip() == __import__("commissure").__version__
    assert not (sol / SKILLS_DIR / "retired-skill").is_symlink()
    assert (own / "SKILL.md").read_text() == "mine\n"
    assert os.readlink(sol / SKILLS_DIR / "shared") == "../../somewhere/else"
    assert (sol / "AGENTS.md").read_text() == "contract\n"
    for skill in kit_skills():
        link = sol / SKILLS_DIR / skill
        assert os.readlink(link) == SKILL_LINK + skill
        assert (link / "SKILL.md").is_file()
    assert not list((sol / ".agents").glob(".commissure.*"))  # no staging folder left


def test_agent_kit_refuses_a_skill_of_the_solutions_own_with_a_kit_name(cli, tmp_path):
    """The check can fail: the solution's own `build-recipe-stage` is never replaced."""
    sol = tmp_path / "sol"
    assert cli("new", str(sol)).exit_code == 0
    link = sol / SKILLS_DIR / "build-recipe-stage"
    link.unlink()
    link.mkdir()
    (link / "SKILL.md").write_text("mine\n")
    (sol / KIT_DIR / "AGENTS.md").write_text("edited\n")

    result = cli("agent-kit", str(sol))
    assert result.exit_code == 2
    assert "build-recipe-stage" in result.stderr
    assert (link / "SKILL.md").read_text() == "mine\n"
    assert (sol / KIT_DIR / "AGENTS.md").read_text() == "edited\n"  # nothing was written


def test_agent_kit_refuses_a_folder_that_is_not_a_solution(cli, tmp_path):
    result = cli("agent-kit", str(tmp_path))
    assert result.exit_code == 2
    assert "not a solution folder" in result.stderr
    assert not (tmp_path / ".agents").exists()


def test_templates_and_kit_are_what_new_writes():
    """The package folders are the single source: every file in them is written, nothing else."""
    assert set(template_files()) == EXPECTED
    # The example keeps the project's files except its recipe and tests, and adds its own.
    example = template_files("example")
    kept = {r for r in EXPECTED if r.split("/")[0] not in ("recipe", "tests")}
    assert kept <= set(example)
    assert {"recipe/recipe.py", "recipe/tally.py", "tests/test_recipe.py"} <= set(example)
    assert not [r for r in example if r.endswith("AGENTS.md") and r != "AGENTS.md"]
    assert all(
        p.is_relative_to(TEMPLATES["example"])
        for r, p in example.items()
        if r.startswith("recipe/")
    )
    assert set(kit_skills()) == SKILLS
    # This repository's own .claude/skills/ links the kit's skills, so core sessions see them.
    for skill in SKILLS:
        assert (
            os.readlink(ROOT / ".claude" / "skills" / skill)
            == f"../../commissure/kit/skills/{skill}"
        )


def _uv(cwd: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["uv", *args], cwd=cwd, env=env, capture_output=True, text=True)


@pytest.mark.skipif(
    os.environ.get("COMMISSURE_SCAFFOLD_INTEGRATION") != "1",
    reason="builds the wheel and runs uv sync (network or a warm uv cache); "
    "set COMMISSURE_SCAFFOLD_INTEGRATION=1",
)
@pytest.mark.parametrize("kind", ["project", "example"])
def test_new_project_syncs_tests_and_lints_against_the_built_wheel(tmp_path, kind):
    """What a developer gets: `new`, `uv sync`, `uv run pytest`, and ruff pass in the project,
    against the wheel as built (so a template or kit file missing from it fails here)."""
    if not shutil.which("uv"):
        pytest.skip("uv is not installed")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    wheels = tmp_path / "wheels"
    built = _uv(ROOT, "build", "--wheel", "-o", str(wheels), env=env)
    assert built.returncode == 0, built.stderr
    sol = tmp_path / "sol"
    env["UV_FIND_LINKS"] = str(wheels)
    # From the wheel, as `uvx commissure new` runs it: no core checkout anywhere.
    wheel = next(wheels.glob("*.whl")).as_posix()
    flag = ["--example"] if kind == "example" else []
    made = _uv(
        tmp_path, "tool", "run", "--from", wheel, "commissure", "new", str(sol), *flag, "--json",
        env=env,
    )  # fmt: skip
    assert made.returncode == 0, made.stderr
    expected = set(template_files(kind))
    # Every template and kit file in this checkout reached the wheel.
    assert set(json.loads(made.stdout)["created"]) == expected | kit_expected()
    for args in (
        ("sync",),
        ("run", "pytest", "-p", "no:cacheprovider"),
        ("run", "ruff", "check", "."),
        ("run", "ruff", "format", "--check", "."),
    ):
        out = _uv(sol, *args, env=env)
        assert out.returncode == 0, (args, out.stdout[-2000:], out.stderr[-2000:])


def test_one_contract_per_solution_and_the_example_shares_its_header():
    """The contract is the solution's AGENTS.md, and nothing else is: no recipe/AGENTS.md. The
    example's contract is finished, but its "Working here" section is the project's, word for
    word, so the two templates can't drift apart."""

    def working_here(text: str) -> str:
        return text[text.index("## Working here") : text.index("## Purpose")]

    project = (TEMPLATES["project"] / "_AGENTS.md").read_text()
    example = (TEMPLATES["example"] / "_AGENTS.md").read_text()
    assert working_here(project) == working_here(example)
    for kind in ("project", "example"):
        sections = [
            line
            for line in TEMPLATES[kind].joinpath("_AGENTS.md").read_text().splitlines()
            if line.startswith("## ")
        ]
        assert sections == [
            "## Working here", "## Purpose", "## Inputs", "## Domain rules",
            "## Quarantine rules", "## Acceptance gate", "## Definition of done",
        ], kind  # fmt: skip
