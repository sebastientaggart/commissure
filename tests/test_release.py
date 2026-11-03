"""commissure/tools/release.py: what ships, what never ships, and when a release is refused.

The unit tests build a tiny repo in tmp_path with a stand-in wheel, so they need no
network and no uv. `test_real_target_installs_and_loads` builds a real target from
`solutions/*/releases/*.toml` with the real wheel and loads the shipped recipe from the
vendored install; it runs when COMMISSURE_RELEASE_INTEGRATION=1, because it locks and
installs a virtualenv.
"""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

from commissure.tools import release

ROOT = Path(__file__).resolve().parents[1]

OPTS = dict(
    run_tests=False, lock=False, smoke=False, sign=False, tag_monorepo=False, allow_dirty=True
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def mono(tmp_path):
    root = tmp_path / "mono"
    sol = root / "solutions" / "alpha"
    write(sol / "recipe" / "recipe.py", "STAGES = []\n")
    write(sol / "recipe" / "extract.py", "X = 1\n")
    write(sol / "recipe" / "AGENTS.md", "internal notes\n")
    write(sol / "recipe" / "__pycache__" / "extract.cpython-312.pyc", "junk")
    write(sol / "tests" / "test_alpha.py", "def test(): pass\n")
    write(sol / "intake" / "batch" / "doc.pdf", "%PDF client data")
    write(sol / "workspace" / "state.db", "db")
    write(sol / "docs" / "findings.md", "internal")
    write(sol / ".env", "SECRET=1\n")
    write(sol / "CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\n- First.\n")
    write(sol / "release.toml", 'version = "0.1.0"\nsummary = "Alpha."\nusage = "Run it."\n'
          'exclude = ["recipe/AGENTS.md"]\n')  # fmt: skip
    write(sol / "releases" / "client.toml", 'title = "Client"\n')
    wheel = write(tmp_path / "wheel" / "commissure-0.1.0-py3-none-any.whl", "wheel bytes")
    return root, wheel, tmp_path / "out"


def shipped(dest: Path) -> set[str]:
    return {p.relative_to(dest).as_posix() for p in dest.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(dest).parts}  # fmt: skip


def test_only_the_allowlist_ships(mono):
    root, wheel, dest = mono
    out, version = release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    assert version == "0.1.0"
    assert shipped(out) == {
        ".gitignore", "README.md", "pyproject.toml", "release.json",
        "vendor/commissure-0.1.0-py3-none-any.whl",
        "solutions/alpha/recipe/recipe.py", "solutions/alpha/recipe/extract.py",
        "solutions/alpha/CHANGELOG.md", "solutions/alpha/intake/README.md",
    }  # fmt: skip
    py = tomllib.loads((out / "pyproject.toml").read_text())
    assert py["project"]["version"] == "0.1.0"
    assert py["project"]["dependencies"] == ["commissure==0.1.0"]
    assert py["tool"]["uv"]["sources"]["commissure"]["path"] == (
        "vendor/commissure-0.1.0-py3-none-any.whl"
    )
    assert "| `alpha` | 0.1.0 | Alpha. |" in (out / "README.md").read_text()
    assert release.git(out, "tag", "--list") == "v0.1.0"
    assert release.git(out, "log", "--format=%s") == (
        "Release v0.1.0: alpha 0.1.0 (commissure 0.1.0)"
    )


def test_nothing_changed_is_refused(mono):
    root, wheel, dest = mono
    release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    with pytest.raises(release.ReleaseError, match="nothing changed"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)


def test_changed_files_need_a_version_bump_and_a_changelog_entry(mono):
    root, wheel, dest = mono
    sol = root / "solutions" / "alpha"
    release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    write(sol / "recipe" / "extract.py", "X = 2\n")
    with pytest.raises(release.ReleaseError, match="bump `version`"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    toml = sol / "release.toml"
    toml.write_text(toml.read_text().replace('"0.1.0"', '"0.2.0"'))
    with pytest.raises(release.ReleaseError, match="no `## 0.2.0` entry"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    write(sol / "CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Changed X.\n\n## 0.1.0\n")
    (sol / "recipe" / "recipe.py").unlink()  # a deleted file must disappear from the release
    write(sol / "recipe" / "recipe2.py", "STAGES = []\n")
    out, version = release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    assert version == "0.2.0"
    assert "solutions/alpha/recipe/recipe.py" not in shipped(out)
    assert (out / "solutions/alpha/recipe/extract.py").read_text() == "X = 2\n"
    assert json.loads((out / "release.json").read_text())["solutions"]["alpha"]["version"] == (
        "0.2.0"
    )
    assert release.git(out, "tag", "--list").split() == ["v0.1.0", "v0.2.0"]


def test_new_release_repo_refuses_an_already_tagged_solution_version(mono, tmp_path):
    """A rebuilt release repo must not reuse a version already tagged on the solution."""
    root, wheel, dest = mono
    sol = root / "solutions" / "alpha"
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["commit", "-q", "-m", "m"],
        ["tag", "-a", "release/client/v0.1.0", "-m", "v0.1.0"],
    ):
        release.git(sol, *args, sign=False)
    with pytest.raises(release.ReleaseError, match="already tagged"):
        release.build(root, "client", dest=tmp_path / "fresh", wheel=wheel, **OPTS)
    toml = sol / "release.toml"
    toml.write_text(toml.read_text().replace('"0.1.0"', '"0.2.0"'))
    write(sol / "CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Second.\n\n## 0.1.0\n")
    _, version = release.build(root, "client", dest=tmp_path / "fresh", wheel=wheel, **OPTS)
    assert version == "0.2.0"


def test_dirty_solution_repo_is_refused(mono):
    root, wheel, dest = mono
    sol = root / "solutions" / "alpha"
    subprocess.run(["git", "init", "-q"], cwd=sol, check=True)
    with pytest.raises(release.ReleaseError, match="uncommitted changes"):
        release.build(root, "client", dest=dest, wheel=wheel, **{**OPTS, "allow_dirty": False})


def test_tarball_is_written_into_solution_releases(mono):
    root, wheel, dest = mono
    release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    [out] = release.publish(root, "client", tarball=True, push=False, dest=dest)
    assert Path(out) == root / "solutions" / "alpha" / "releases" / "client-v0.1.0.tar.gz"
    with tarfile.open(out) as tar:
        names = set(tar.getnames())
    assert "client-v0.1.0/solutions/alpha/recipe/extract.py" in names
    assert not any(
        "tests" in n or "intake/batch" in n or ".env" in n or "/releases/" in n for n in names
    )
    with pytest.raises(release.ReleaseError, match="no remote"):
        release.publish(root, "client", tarball=False, push=True, dest=dest)


def test_ambiguous_or_multi_solution_target_is_refused(mono):
    root, wheel, dest = mono
    write(root / "solutions" / "beta" / "releases" / "client.toml", 'title = "Beta"\n')
    with pytest.raises(release.ReleaseError, match="multiple solutions"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    out, version = release.build(root, "alpha/client", dest=dest, wheel=wheel, **OPTS)
    assert version == "0.1.0"
    assert out == dest

    write(
        root / "solutions" / "alpha" / "releases" / "client.toml",
        'title = "Client"\nsolutions = ["alpha", "beta"]\n',
    )
    with pytest.raises(release.ReleaseError, match="may only package alpha"):
        release.build(root, "alpha/client", dest=dest, wheel=wheel, **OPTS)


def forbid_in_target(root: Path, pattern: str) -> None:
    lines = ['title = "Client"', f"forbid = ['{pattern}']"]
    write(root / "solutions" / "alpha" / "releases" / "client.toml", "\n".join(lines) + "\n")


def test_other_solutions_are_forbidden_automatically(mono):
    """Any sibling solution in solutions/ is forbidden without listing it."""
    root, wheel, dest = mono
    (root / "solutions" / "acme-other_office").mkdir(parents=True)
    write(root / "solutions" / "alpha" / "recipe" / "extract.py", "X = 1\n# see Other Office\n")
    with pytest.raises(release.ReleaseError, match=r"solutions/alpha/recipe/extract.py:2"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    assert release.git(dest, "tag", "--list") == ""


def test_forbidden_name_in_a_shipped_file_is_refused(mono):
    """Another office's solution named in shipped code: refused, nothing committed."""
    root, wheel, dest = mono
    forbid_in_target(root, r"(?i)\bother[ _-]?office\b")
    write(root / "solutions" / "alpha" / "recipe" / "extract.py", "X = 1\n# see Other Office\n")
    with pytest.raises(release.ReleaseError, match=r"solutions/alpha/recipe/extract.py:2"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    assert release.git(dest, "tag", "--list") == ""


def test_forbidden_text_inside_the_vendored_wheel_is_refused(mono, tmp_path):
    """The wheel's README ships too (as METADATA); it must not name other solutions."""
    import zipfile

    root, _, dest = mono
    forbid_in_target(root, r"(?i)\bother[ _-]?project\b")
    wheel = tmp_path / "w" / "commissure-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr(
            "commissure-0.1.0.dist-info/METADATA", "Name: commissure\n\nSee Other Project.\n"
        )
    with pytest.raises(release.ReleaseError, match=r"commissure-0.1.0.dist-info/METADATA:3"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)


def test_ds_store_inside_the_vendored_wheel_is_refused(mono, tmp_path):
    """OS metadata files (.DS_Store, __pycache__) inside the wheel must be refused."""
    import zipfile

    root, _, dest = mono
    wheel = tmp_path / "w" / "commissure-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("commissure/.DS_Store", "\x00\x00\x00\x01Bud1")
    with pytest.raises(release.ReleaseError, match=r"commissure/\.DS_Store: forbidden file"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)


def test_release_module_inside_the_vendored_wheel_passes_scan(mono, tmp_path):
    """commissure/tools/release.py ships in the wheel; DEFAULT_FORBID must not match it."""
    import zipfile

    root, _, dest = mono
    wheel = tmp_path / "w" / "commissure-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr(
            "commissure/tools/release.py",
            (ROOT / "commissure" / "tools" / "release.py").read_text(encoding="utf-8"),
        )
    _, version = release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    assert version == "0.1.0"


def test_release_cli_resolves_target_from_solution_dir_and_reports_errors(cli, mono, monkeypatch):
    root, _, _ = mono
    monkeypatch.chdir(root / "solutions" / "alpha")
    res = cli("release", "publish", "client", "--tarball")
    assert res.exit_code == 1
    assert "no release of client has been built" in res.stderr


def test_builtin_and_local_forbid_apply_to_every_target(mono):
    root, wheel, dest = mono
    write(
        root / "solutions" / "alpha" / "recipe" / "extract.py",
        'PATH = "/Users/alice/secret"\n',
    )
    with pytest.raises(release.ReleaseError, match="1 forbidden match"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)

    # A failed build leaves uncommitted files in dest for inspection; fixing the leak and
    # retrying must not treat the failed attempt as an already-released v0.1.0.
    # Cross-solution slugs in solutions/.forbid.toml forbid other solutions even when not
    # checked out locally, while skipping the target solution's own name.
    write(
        root / "solutions" / ".forbid.toml",
        r"forbid = ['internal\.example\.org', '(?i)\balpha\b', '(?i)\bother[ _-]?office\b']"
        "\n",
    )
    write(
        root / "solutions" / "alpha" / "recipe" / "extract.py",
        'URL = "https://internal.example.org"\n',
    )
    with pytest.raises(release.ReleaseError, match="1 forbidden match"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)

    write(
        root / "solutions" / "alpha" / "recipe" / "extract.py",
        "X = 1\n# see Other Office\n",
    )
    with pytest.raises(release.ReleaseError, match=r"solutions/alpha/recipe/extract.py:2"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)

    write(root / "solutions" / "alpha" / "recipe" / "extract.py", "X = 1\n")
    _, version = release.build(root, "client", dest=dest, wheel=wheel, **OPTS)
    assert version == "0.1.0"


def test_bad_forbid_regex_is_refused(mono):
    root, wheel, dest = mono
    write(
        root / "solutions" / "alpha" / "releases" / "client.toml", 'title = "C"\nforbid = ["("]\n'
    )
    with pytest.raises(release.ReleaseError, match="is not a regex"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)


def test_todo_release_text_is_refused(mono):
    root, wheel, dest = mono
    write(root / "solutions" / "alpha" / "release.toml",
          'version = "0.1.0"\nsummary = "TODO: say what it does"\nusage = "Run it."\n')  # fmt: skip
    with pytest.raises(release.ReleaseError, match="still says TODO"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)


def test_missing_changelog_entry_or_include_is_refused(mono):
    root, wheel, dest = mono
    sol = root / "solutions" / "alpha"
    write(sol / "release.toml", 'version = "0.1.0"\nsummary = "A."\nusage = "U."\n'
          'include = ["recipe", "nope.md"]\n')  # fmt: skip
    with pytest.raises(release.ReleaseError, match="`nope.md` does not exist"):
        release.build(root, "client", dest=dest, wheel=wheel, **OPTS)


@pytest.mark.skipif(
    os.environ.get("COMMISSURE_RELEASE_INTEGRATION") != "1",
    reason="set COMMISSURE_RELEASE_INTEGRATION=1 to build and install a real release",
)
def test_real_target_installs_and_loads(tmp_path):
    targets = sorted((ROOT / "solutions").glob("*/releases/*.toml"))
    if not targets:
        pytest.skip("no solution targets checked out under solutions/*/releases/")
    target_path = targets[0]
    sol_name = target_path.parents[1].name
    target_name = target_path.stem
    dest, version = release.build(
        ROOT, f"{sol_name}/{target_name}", dest=tmp_path / target_name, run_tests=False,
        sign=False, allow_dirty=True, tag_monorepo=False,
    )  # fmt: skip
    files = shipped(dest)
    assert version
    assert not any("/tests/" in f or "/workspace/" in f or f.endswith("AGENTS.md") for f in files)
    assert not any(f.startswith("solutions/") and "/intake/" in f and not f.endswith(
        "intake/README.md") for f in files)  # fmt: skip
    assert {f"solutions/{sol_name}/recipe/recipe.py", "uv.lock"} <= files
