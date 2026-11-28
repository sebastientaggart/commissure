"""commissure/tools/wrapper.py (`commissure dev`): a solution developed outside this repository.

The unit tests use a tiny synthetic core repository and local bare remotes in tmp_path, and
skip `uv` (`do_sync=False`), so they need no network. `test_real_wrapper_syncs_and_tests`
pins this checkout's HEAD and runs `uv sync` and `uv run pytest` in the wrapper; it runs when
COMMISSURE_WRAPPER_INTEGRATION=1, because it installs a virtualenv.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

from commissure.scaffold import scaffold_solution
from commissure.tools import release, wrapper

ROOT = Path(__file__).resolve().parents[1]
OFF = dict(do_sync=False)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def git_env(monkeypatch, tmp_path):
    # No user config (signing, hooks, default branch) and a fixed identity: a CI runner has none.
    gitconfig = write(tmp_path / "gitconfig", "[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Wrapper Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "wrapper-test@example.invalid")


CHANGELOG_1 = "# Changelog\n\n## Unreleased\n\n- **Added:** first.\n"
# A blank added line and a line starting with "+" must both survive the diff parse.
CHANGELOG_2 = (
    "# Changelog\n\n## Unreleased\n\n- **Upgrading:** rename `a` to `b`.\n\n"
    "+ plus-led line\n- **Added:** first.\n"
)
ADDED_1_TO_2 = ["- **Upgrading:** rename `a` to `b`.", "", "+ plus-led line"]


@pytest.fixture
def core(tmp_path):
    """A stand-in core repository with two commits; returns (path, rev1, rev2)."""
    repo = tmp_path / "core"
    write(repo / "AGENTS.md", "# Core rules\n")
    write(repo / "README.md", "# Core\n")
    write(repo / "docs" / "guide.md", "guide\n")
    write(repo / "docs" / "solutions.md", "solutions\n")
    for skill in ("build-recipe-stage", "investigate-solution", "release-target"):
        write(repo / ".agents" / "skills" / skill / "SKILL.md", f"# {skill}\n")
    write(repo / ".github" / "ISSUE_TEMPLATE" / "upstream-request.md", "## Need\n")
    write(repo / ".github" / "workflows" / "ci.yml", "not in the snapshot\n")
    write(repo / "commissure" / "__init__.py", "not in the snapshot\n")
    write(repo / "CHANGELOG.md", CHANGELOG_1)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    rev1 = git(repo, "rev-parse", "HEAD")
    write(repo / "CHANGELOG.md", CHANGELOG_2)
    write(repo / "docs" / "guide.md", "guide, revised\n")
    git(repo, "commit", "-q", "-am", "two")
    return repo, rev1, git(repo, "rev-parse", "HEAD")


def bare(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "remotes" / f"{name}.git"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


@pytest.fixture
def remote(tmp_path):
    """A solution remote with one commit, made by `commissure init`."""
    src = tmp_path / "src" / "alpha"
    scaffold_solution(src, "alpha")
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "Initial commit")
    url = bare(tmp_path, "alpha")
    git(src, "push", "-q", str(url), "HEAD:main")
    return url


@pytest.fixture
def wrapped(tmp_path, core, remote):
    repo, rev1, rev2 = core
    dest = wrapper.clone(str(remote), tmp_path / "w", rev=rev1, core_url=f"file://{repo}", **OFF)
    return dest, repo, rev1, rev2


def archive(repo: Path, rev: str) -> dict[str, bytes]:
    data = subprocess.run(
        ["git", "-C", str(repo), "archive", rev, *[p for p in wrapper.SNAPSHOT]],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def digest(root: Path) -> dict[str, str]:
    """Every file and link under `root`, .git included, with its bytes or link target."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = "link:" + os.readlink(p)
        elif p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --- clone ---------------------------------------------------------------------------------


def test_clone_snapshots_core_read_only_at_the_pin(wrapped):
    dest, repo, rev1, _ = wrapped
    snap = tree(dest / ".core")
    assert snap.pop("REVISION") == f"{rev1}\n".encode()
    assert snap == archive(repo, rev1)
    assert "commissure/__init__.py" not in snap and ".github/workflows/ci.yml" not in snap
    for p in [dest / ".core", *(dest / ".core").rglob("*")]:
        assert not os.access(p, os.W_OK), p
    assert git(dest / "solutions" / "alpha", "log", "--format=%s") == "Initial commit"


def test_clone_writes_the_generated_files(wrapped):
    dest, repo, rev1, _ = wrapped
    py = tomllib.loads((dest / "pyproject.toml").read_text())
    assert py["tool"]["uv"]["sources"]["commissure"] == {"git": f"file://{repo}", "rev": rev1}
    assert py["tool"]["commissure-dev"]["solution"] == "alpha"
    assert py["tool"]["pytest"]["ini_options"]["testpaths"] == ["solutions/alpha/tests"]
    assert (dest / "CLAUDE.md").read_text() == "@AGENTS.md\n@.core/AGENTS.md\n"
    assert (dest / ".python-version").read_text() == "3.12\n"
    agents = (dest / "AGENTS.md").read_text()
    assert "solutions/alpha/" in agents and "uv run commissure dev repin" in agents
    assert "gh issue create -R sebastientaggart/commissure" in agents
    settings = (dest / ".claude" / "settings.json").read_text()
    assert '"Edit(/.core/**)"' in settings and '"Bash(gh issue create:*)"' in settings
    links = sorted(p.name for p in (dest / ".claude" / "skills").iterdir())
    assert links == ["build-recipe-stage", "investigate-solution"]  # not release-target
    assert (dest / ".claude/skills/build-recipe-stage/SKILL.md").read_text() == (
        "# build-recipe-stage\n"
    )


def test_clone_refuses_a_short_or_unknown_rev_and_writes_nothing(tmp_path, core, remote):
    repo, rev1, _ = core
    url = f"file://{repo}"
    with pytest.raises(wrapper.UsageError, match="full 40-character"):
        wrapper.clone(str(remote), tmp_path / "w", rev=rev1[:7], core_url=url, **OFF)
    with pytest.raises(wrapper.UsageError, match="no commit"):
        wrapper.clone(str(remote), tmp_path / "w", rev="0" * 40, core_url=url, **OFF)
    assert not (tmp_path / "w").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".w.")]  # no staging left


def test_clone_refuses_a_folder_with_files(tmp_path, core, remote):
    repo, rev1, _ = core
    write(tmp_path / "w" / "keep.txt", "mine")
    with pytest.raises(wrapper.UsageError, match="not empty"):
        wrapper.clone(str(remote), tmp_path / "w", rev=rev1, core_url=f"file://{repo}", **OFF)
    assert (tmp_path / "w" / "keep.txt").read_text() == "mine"


def test_a_non_file_core_url_is_read_through_a_mirror(tmp_path, core, remote):
    repo, rev1, rev2 = core
    core_remote = bare(tmp_path, "core")
    git(repo, "push", "-q", str(core_remote), "main")
    # A plain path stands in for an https URL: anything not file:// is mirrored.
    dest = wrapper.clone(str(remote), tmp_path / "w", rev=rev1, core_url=str(core_remote), **OFF)
    assert (dest / ".core.git" / "HEAD").is_file()
    assert tree(dest / ".core").keys() - {"REVISION"} == archive(repo, rev1).keys()
    write(repo / "CHANGELOG.md", CHANGELOG_2 + "- **Added:** pushed later.\n")
    git(repo, "commit", "-q", "-am", "three")
    rev3 = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", str(core_remote), "main")
    result = wrapper.repin(dest, rev3, run_tests=False, echo=lambda _: None, **OFF)
    assert result.added == [*ADDED_1_TO_2, "- **Added:** pushed later."]


# --- repin ---------------------------------------------------------------------------------


def test_repin_reports_exactly_the_changelog_lines_added(wrapped):
    dest, repo, rev1, rev2 = wrapped
    printed: list[str] = []
    result = wrapper.repin(dest, rev2, run_tests=False, echo=printed.append, **OFF)
    assert result.added == ADDED_1_TO_2
    assert printed == [f"== core CHANGELOG.md, added since {rev1}", *ADDED_1_TO_2]
    assert wrapper.read_pin(dest)[2] == rev2
    assert (dest / ".core" / "REVISION").read_text() == f"{rev2}\n"
    assert (dest / ".core" / "docs" / "guide.md").read_text() == "guide, revised\n"


@pytest.mark.parametrize("sha", ["abc1234", "0" * 40])
def test_repin_refuses_a_short_or_unknown_sha_and_writes_nothing(wrapped, sha):
    dest, *_ = wrapped
    before = digest(dest)
    with pytest.raises(wrapper.UsageError):
        wrapper.repin(dest, sha, run_tests=False, echo=lambda _: None, **OFF)
    assert digest(dest) == before


def test_repin_puts_the_pin_back_when_sync_fails(wrapped, monkeypatch):
    dest, _, rev1, rev2 = wrapped
    write(dest / "uv.lock", "old lock\n")
    before = digest(dest)

    def failing_sync(w, *, upgrade=False):
        write(w / "uv.lock", "half-written lock\n")
        raise wrapper.WrapperError("uv lock failed: no network")

    restored = []
    monkeypatch.setattr(wrapper, "sync", failing_sync)
    monkeypatch.setattr(wrapper, "restore_env", restored.append)
    with pytest.raises(wrapper.WrapperError, match="no network"):
        wrapper.repin(dest, rev2, run_tests=False, echo=lambda _: None)
    assert digest(dest) == before
    assert wrapper.read_pin(dest)[2] == rev1
    assert restored == [dest]


def test_cli_repin_exits_2_on_a_short_sha(wrapped, cli, monkeypatch):
    dest, *_ = wrapped
    monkeypatch.chdir(dest / "solutions" / "alpha")
    result = cli("dev", "repin", "abc1234")
    assert result.exit_code == 2, result.output
    assert "full 40-character" in result.output


# --- refresh -------------------------------------------------------------------------------


def test_refresh_leaves_solutions_and_local_files_byte_identical(wrapped):
    dest, *_ = wrapped
    sol = dest / "solutions" / "alpha"
    # Ignored (workspace, intake, .env), untracked, and modified tracked files.
    write(sol / "workspace" / "state.db", "verdicts")
    write(sol / "workspace" / "state.db-wal", "wal")
    write(sol / "intake" / "batch" / "doc.pdf", "%PDF")
    write(sol / ".env", "SECRET=1\n")
    write(sol / "scratch.txt", "untracked")
    write(sol / "README.md", "edited, not committed\n")
    (sol / "obsidian-notes").symlink_to(dest.parent)
    write(dest / "AGENTS.local.md", "# This machine\n")
    write(dest / ".claude" / "settings.local.json", '{"permissions": {}}\n')
    (dest / ".claude" / "skills" / "other").symlink_to("../../elsewhere/other")
    write(dest / "AGENTS.md", "hand-edited\n")
    sol_before = digest(sol)
    local_before = {
        p: (dest / p).read_bytes() for p in ("AGENTS.local.md", ".claude/settings.local.json")
    }

    wrapper.refresh(dest, **OFF)

    assert digest(sol) == sol_before
    assert {p: (dest / p).read_bytes() for p in local_before} == local_before
    assert os.readlink(dest / ".claude" / "skills" / "other") == "../../elsewhere/other"
    assert (dest / "AGENTS.md").read_text().startswith("# Solution wrapper: alpha")
    assert (dest / "CLAUDE.md").read_text().splitlines()[-1] == "@AGENTS.local.md"


def test_an_unscaffolded_solution_gets_new_solution_until_it_has_a_recipe(wrapped, core):
    dest, repo, *_ = wrapped
    write(repo / ".agents" / "skills" / "new-solution" / "SKILL.md", "# new-solution\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "skill")
    wrapper.repin(dest, git(repo, "rev-parse", "HEAD"), run_tests=False, echo=lambda _: None, **OFF)
    recipe = dest / "solutions" / "alpha" / "recipe"
    recipe.rename(recipe.with_name("recipe-aside"))  # a repository holding only an idea
    wrapper.refresh(dest, **OFF)
    links = dest / ".claude" / "skills"
    assert (links / "new-solution" / "SKILL.md").is_file()
    assert "Not scaffolded yet" in (dest / "AGENTS.md").read_text()
    recipe.with_name("recipe-aside").rename(recipe)
    wrapper.refresh(dest, **OFF)
    assert not (links / "new-solution").exists()
    assert "Not scaffolded yet" not in (dest / "AGENTS.md").read_text()


def test_refresh_rebuilds_a_missing_snapshot_at_the_pin(wrapped):
    dest, repo, rev1, _ = wrapped
    wrapper._rmtree(dest / ".core")
    wrapper.refresh(dest, **OFF)
    snap = tree(dest / ".core")
    assert snap.pop("REVISION") == f"{rev1}\n".encode()
    assert snap == archive(repo, rev1)


# --- new -----------------------------------------------------------------------------------


def test_new_pushes_one_commit_to_an_empty_remote(tmp_path, core):
    repo, rev1, _ = core
    url = bare(tmp_path, "beta")
    dest = wrapper.new(
        tmp_path / "w", "beta", rev=rev1, core_url=f"file://{repo}", remote=str(url), **OFF
    )
    assert git(url, "rev-list", "--count", "main") == "1"
    sol = dest / "solutions" / "beta"
    assert git(sol, "rev-parse", "HEAD") == git(url, "rev-parse", "main")
    assert (sol / "recipe" / "recipe.py").is_file()
    assert git(sol, "status", "--porcelain") == ""
    assert wrapper.read_pin(dest) == ("beta", f"file://{repo}", rev1)


def test_new_without_a_remote_commits_locally(tmp_path, core):
    repo, rev1, _ = core
    dest = wrapper.new(tmp_path / "w", "beta", rev=rev1, core_url=f"file://{repo}", **OFF)
    sol = dest / "solutions" / "beta"
    assert git(sol, "rev-list", "--count", "HEAD") == "1"
    assert git(sol, "remote") == ""


def test_new_refuses_a_remote_with_commits_and_writes_nothing(tmp_path, core, remote):
    repo, rev1, _ = core
    before = git(remote, "rev-parse", "main")
    with pytest.raises(wrapper.UsageError, match="already has commits"):
        wrapper.new(
            tmp_path / "w", "alpha", rev=rev1, core_url=f"file://{repo}", remote=str(remote), **OFF
        )
    assert not (tmp_path / "w").exists()
    assert git(remote, "rev-parse", "main") == before
    assert git(remote, "rev-list", "--count", "main") == "1"


# --- what the generated files may contain --------------------------------------------------


def generated(dest: Path) -> list[Path]:
    keep = {"solutions", ".core", ".core.git", ".venv"}
    return [
        p
        for p in dest.rglob("*")
        if p.is_file() and not p.is_symlink() and not keep & set(p.relative_to(dest).parts)
    ]


def test_generated_files_hold_no_machine_paths(wrapped, tmp_path):
    dest, repo, rev1, _ = wrapped
    # The documented exception: a file:// pin names its checkout, in pyproject.toml only.
    for path in generated(dest):
        text = path.read_text(encoding="utf-8")
        if path.name != "pyproject.toml":
            assert str(tmp_path) not in text, path
    # With the default (public) core URL, nothing names this machine, and the release scan
    # (built-in credential and path patterns) finds nothing.
    out = tmp_path / "public"
    for rel, text in wrapper.render(dest, "alpha", rev1, wrapper.public_url()).items():
        write(out / rel, text)
        assert str(tmp_path) not in text and str(Path.home()) not in text, rel
    assert release.scan_forbidden(out, release.DEFAULT_FORBID) == []


def test_machine_paths_would_be_caught(tmp_path):
    # The scan above must be able to fail: a home-directory path is a hit.
    write(tmp_path / "x" / "AGENTS.md", "see /Users/someone/core\n")
    assert release.scan_forbidden(tmp_path / "x", release.DEFAULT_FORBID)


def test_dev_dependencies_match_cores():
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert list(wrapper.DEV_DEPS) == py["dependency-groups"]["dev"]


def test_snapshot_paths_exist_in_this_repository():
    # A renamed doc folder or template would silently drop out of every wrapper's snapshot.
    for rel in wrapper.SNAPSHOT:
        assert (ROOT / rel).exists(), rel
    for skill in wrapper.SKILLS:
        assert (ROOT / ".agents" / "skills" / skill / "SKILL.md").is_file(), skill


def test_the_rev_defaults_to_the_running_commissure(monkeypatch):
    sha = "1" * 40
    monkeypatch.setattr(wrapper, "running_source", lambda: ("https://example.invalid/c.git", sha))
    assert wrapper.resolve_pin(None, None) == ("https://example.invalid/c.git", sha)
    assert wrapper.resolve_pin(None, "file:///elsewhere") == ("file:///elsewhere", sha)
    # An explicit rev without a URL means the public repository, not the running source.
    assert wrapper.resolve_pin("2" * 40, None) == (wrapper.public_url(), "2" * 40)
    monkeypatch.setattr(wrapper, "running_source", lambda: (None, None))
    with pytest.raises(wrapper.UsageError, match="pass --rev"):
        wrapper.resolve_pin(None, None)


def test_snapshot_is_not_writable_by_its_owner(wrapped):
    dest, *_ = wrapped
    mode = (dest / ".core" / "AGENTS.md").stat().st_mode
    assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


# --- integration ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("COMMISSURE_WRAPPER_INTEGRATION") != "1",
    reason="set COMMISSURE_WRAPPER_INTEGRATION=1 to sync a real wrapper with uv",
)
def test_real_wrapper_syncs_and_tests(tmp_path, remote):
    head = git(ROOT, "rev-parse", "HEAD")
    # A freshly init-ed solution has no tests; give it one so pytest has something to pass.
    src = tmp_path / "add-test"
    subprocess.run(["git", "clone", "-q", str(remote), str(src)], check=True)
    write(src / "tests" / "test_smoke.py", "import commissure.api\n\n\ndef test_ok():\n    pass\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "Add a test")
    git(src, "push", "-q", "origin", "main")
    dest = wrapper.clone(str(remote), tmp_path / "w", rev=head, core_url=f"file://{ROOT}")
    out = subprocess.run(["uv", "run", "pytest", "-q", "-n0"], cwd=dest, capture_output=True,
                         text=True, check=False)  # fmt: skip
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 passed" in out.stdout
    where = subprocess.run(
        ["uv", "run", "python", "-c", "import commissure; print(commissure.__file__)"],
        cwd=dest, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert str(dest / ".venv") in where  # the pinned install, not this checkout
