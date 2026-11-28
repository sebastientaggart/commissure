"""Wrappers: develop one solution outside this repository, against a pinned core.

    commissure dev clone <solution-remote> <dir> [--rev SHA] [--core-url URL] [--name NAME]
    commissure dev new <dir> --name <name> [--remote URL] [--rev SHA] [--core-url URL]
    commissure dev repin <full-sha>      # inside a wrapper
    commissure dev refresh               # inside a wrapper

A Wrapper has the shape a client release has: one solution at `solutions/<name>/` (its own
git repository, never touched by these commands after it is cloned or created), core pinned
by commit in a generated `pyproject.toml`, and core's rules, docs, skills, and changelog at
that same commit in a read-only snapshot, `.core/`. Generated beside them: `AGENTS.md`
(solution-owner rules), `CLAUDE.md`, `.claude/settings.json` (denies writing `.core/`,
`.core.git/`, `.venv/`, and filing issues), skill links into the snapshot, and
`.python-version`. `refresh` rewrites those files and leaves everything under `solutions/`
alone.

Machine-specific additions never go into generated files: `AGENTS.local.md` (imported by the
generated `CLAUDE.md` when it exists at generation time) and `.claude/settings.local.json`
(merged by Claude Code) are written by the person or their own tooling, and no command here
writes or removes them.

Core's history comes from where the pin points: a `file://` checkout is read in place; any
other URL gets a blobless mirror in `.core.git/`. The snapshot is `git archive` at the pin.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from string import Template
from urllib.parse import urlparse

TEMPLATE = Path(__file__).parent / "wrapper_template"
# Paths copied into .core/ when the pinned revision has them. The issue template is there so
# an upstream draft can follow it; CHANGELOG.md is what `repin` diffs.
SNAPSHOT = ("AGENTS.md", "README.md", "CHANGELOG.md", "docs", ".agents", ".github/ISSUE_TEMPLATE")
# Skills a solution owner uses. new-solution is not one: `dev new` scaffolds; release-target
# needs a core checkout, so releases stay with the core maintainer.
SKILLS = ("build-recipe-stage", "investigate-solution")
# Core's own dev group (pyproject.toml), so tests and lint behave the same in a wrapper.
# tests/test_wrapper.py fails when the two drift.
DEV_DEPS = ("pytest>=9.1.1", "pytest-xdist>=3.8.0", "ruff>=0.17.2")
# Every write rule below is relative to the wrapper (`/x` in a project settings file is
# relative to the project root), so the file holds no machine path and the folder can move.
# `Edit` rules cover every file-writing tool. Filing an issue on core is public the moment it
# happens, so a person does it.
DENY = ("Edit(/.core/**)", "Edit(/.core.git/**)", "Edit(/.venv/**)", "Bash(gh issue create:*)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SKILL_LINK = "../../.core/.agents/skills/"


class WrapperError(Exception):
    """A wrapper command that must not run as asked. Raised before anything is written,
    except where a function's docstring says otherwise."""


class UsageError(WrapperError):
    """Bad input (a short or unknown sha, a folder that exists): exit code 2."""


@dataclass
class Repin:
    old: str | None
    new: str
    added: list[str]  # CHANGELOG.md lines added between the two pins, in order
    tests: int | None  # pytest's exit code, or None when not run


# --- small helpers -------------------------------------------------------------------------


def _run(*cmd: str | Path, cwd: Path | None = None, binary: bool = False) -> str | bytes:
    out = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, capture_output=True, text=not binary, check=False
    )
    if out.returncode != 0:
        err = out.stderr if isinstance(out.stderr, str) else out.stderr.decode("utf-8", "replace")
        raise WrapperError(f"{' '.join(map(str, cmd))} failed: {err.strip()}")
    return out.stdout


def _git(repo: Path, *args: str) -> str:
    return _run("git", "-C", repo, *args)


def _ok(*cmd: str | Path) -> bool:
    return subprocess.run([str(c) for c in cmd], capture_output=True, check=False).returncode == 0


def _check_sha(rev: str) -> str:
    if not SHA_RE.fullmatch(rev):
        raise UsageError(f"need a full 40-character commit sha, got {rev!r}")
    return rev


def _check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise UsageError(f"solution name {name!r} must be letters, digits, '.', '_', or '-'")
    return name


def _make_writable(path: Path) -> None:
    if not path.exists():
        return
    for p in [path, *path.rglob("*")]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode | stat.S_IWUSR)


def _rmtree(path: Path) -> None:
    """Remove a tree this module created, read-only snapshot included."""
    _make_writable(path)
    shutil.rmtree(path)


# --- where core comes from -----------------------------------------------------------------


def _meta_url(label: str) -> str:
    for entry in metadata.metadata("commissure").get_all("Project-URL") or []:
        key, _, url = entry.partition(",")
        if key.strip().lower() == label.lower():
            return url.strip()
    raise WrapperError(f"commissure's package metadata has no {label} URL")


def public_url() -> str:
    """Core's public git URL, from the package metadata."""
    return _meta_url("Repository").rstrip("/") + ".git"


def issues_repo() -> str:
    """OWNER/REPO of core's public issue tracker, for `gh issue create -R`."""
    parts = urlparse(_meta_url("Issues")).path.strip("/").split("/")
    return "/".join(parts[:2])


def running_source() -> tuple[str | None, str | None]:
    """(git URL, commit) of the running commissure, when its install records one.

    A git install (uv, pip, uvx --from git+...) records both in `direct_url.json`. An
    editable checkout gives its HEAD, but only when tracked files are clean, because the
    templates running now would not be the ones at that commit. Anything else: (None, None).
    """
    try:
        raw = metadata.distribution("commissure").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None, None
    info = json.loads(raw) if raw else {}
    vcs = info.get("vcs_info") or {}
    if vcs.get("vcs") == "git" and SHA_RE.fullmatch(vcs.get("commit_id", "")):
        return info["url"], vcs["commit_id"]
    url = info.get("url", "")
    if (info.get("dir_info") or {}).get("editable") and url.startswith("file://"):
        checkout = Path(urlparse(url).path)
        if (checkout / ".git").exists():
            if _git(checkout, "status", "--porcelain", "--untracked-files=no").strip():
                raise UsageError(
                    f"the running commissure is an editable checkout with uncommitted changes "
                    f"({checkout}); commit them or pass --rev"
                )
            return None, _git(checkout, "rev-parse", "HEAD").strip()
    return None, None


def resolve_pin(rev: str | None, core_url: str | None) -> tuple[str, str]:
    """(core URL, rev): given values win; else the running commissure's, else the public URL."""
    if rev is None:
        src_url, rev = running_source()
        if rev is None:
            raise UsageError("can't tell which core commit is running; pass --rev <full-sha>")
        core_url = core_url or src_url
    return core_url or public_url(), _check_sha(rev)


def core_git(wrapper: Path, url: str) -> Path:
    """A git directory holding core's history at `url`.

    A `file://` URL is read in place. Any other URL is mirrored (blobless, about 1 MB) into
    `<wrapper>/.core.git/` and fetched, so a pin pushed since the last run is found.
    """
    if url.startswith("file://"):
        path = Path(urlparse(url).path)
        if not path.exists():
            raise UsageError(f"no core repository at {url}")
        return path
    mirror = wrapper / ".core.git"
    if not mirror.is_dir():
        _run("git", "clone", "-q", "--mirror", "--filter=blob:none", url, mirror)
    else:
        _git(mirror, "fetch", "-q", "--prune", "origin")
    return mirror


def _require_commit(git_dir: Path, rev: str, url: str) -> None:
    if not _ok("git", "-C", git_dir, "cat-file", "-e", f"{rev}^{{commit}}"):
        raise UsageError(f"no commit {rev} at {url} (pushed?)")


# --- generated files -----------------------------------------------------------------------


def snapshot(wrapper: Path, git_dir: Path, rev: str) -> None:
    """Replace `<wrapper>/.core/` with `git archive` of SNAPSHOT at `rev`, read-only.

    The new tree is built beside the old one and swapped in, so a failure leaves the old
    snapshot whole.
    """
    present = [p for p in SNAPSHOT if _ok("git", "-C", git_dir, "cat-file", "-e", f"{rev}:{p}")]
    data = _run("git", "-C", git_dir, "archive", "--format=tar", rev, *present, binary=True)
    fresh = wrapper / ".core.new"
    if fresh.exists():
        _rmtree(fresh)
    fresh.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        tar.extractall(fresh, filter="data")
    (fresh / "REVISION").write_text(rev + "\n", encoding="utf-8")
    for p in sorted(fresh.rglob("*"), reverse=True) + [fresh]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    old = wrapper / ".core"
    if old.exists():
        _rmtree(old)
    fresh.rename(old)


def pinned_revision(wrapper: Path) -> str | None:
    rev = wrapper / ".core" / "REVISION"
    return rev.read_text(encoding="utf-8").strip() if rev.is_file() else None


def render(wrapper: Path, name: str, rev: str, core_url: str) -> dict[str, str]:
    """The generated text files, by path relative to the wrapper."""
    tmpl = {p.name.removesuffix(".tmpl"): p.read_text(encoding="utf-8")
            for p in TEMPLATE.glob("*.tmpl")}  # fmt: skip
    skills = [s for s in SKILLS if (wrapper / ".core/.agents/skills" / s).is_dir()]
    project = re.sub(r"[^a-z0-9._-]+", "-", f"{name}-dev".lower())
    imports = ["@AGENTS.md", "@.core/AGENTS.md"]
    if (wrapper / "AGENTS.local.md").is_file():
        imports.append("@AGENTS.local.md")
    return {
        "pyproject.toml": Template(tmpl["pyproject.toml"]).substitute(
            project=project,
            core_url=core_url,
            rev=rev,
            name=name,
            dev_deps="".join(f'    "{d}",\n' for d in DEV_DEPS),
        ),
        "AGENTS.md": Template(tmpl["AGENTS.md"]).substitute(
            name=name,
            skills=", ".join(f"`{s}`" for s in skills) or "none in this snapshot",
            issues_repo=issues_repo(),
        ),
        "CLAUDE.md": "\n".join(imports) + "\n",
        ".python-version": "3.12\n",
        ".claude/settings.json": json.dumps({"permissions": {"deny": list(DENY)}}, indent=2) + "\n",
    }


def write_generated(wrapper: Path, name: str, rev: str, core_url: str) -> None:
    """Write every generated file. Needs `.core/` in place (skill links point into it)."""
    for rel, text in render(wrapper, name, rev, core_url).items():
        path = wrapper / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    links = wrapper / ".claude" / "skills"
    links.mkdir(parents=True, exist_ok=True)
    # Only links into the snapshot are ours; anything else here belongs to someone else.
    for old in links.iterdir():
        if old.is_symlink() and os.readlink(old).startswith(SKILL_LINK):
            old.unlink()
    for s in SKILLS:
        if (wrapper / ".core/.agents/skills" / s).is_dir():
            (links / s).symlink_to(SKILL_LINK + s)


def sync(wrapper: Path, *, upgrade: bool = False) -> None:
    lock = ["uv", "lock", "--upgrade-package", "commissure"] if upgrade else ["uv", "lock"]
    _run(*lock, cwd=wrapper)
    _run("uv", "sync", cwd=wrapper)


def restore_env(wrapper: Path) -> None:
    """Best effort: put `.venv/` back in line with the restored lock after a failed repin."""
    subprocess.run(["uv", "sync"], cwd=wrapper, capture_output=True, check=False)


# --- wrapper lookup ------------------------------------------------------------------------


def find_wrapper(start: Path | None = None) -> Path:
    """The nearest folder at or above `start` whose pyproject.toml names a wrapper's solution."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        py = folder / "pyproject.toml"
        if py.is_file():
            conf = tomllib.loads(py.read_text(encoding="utf-8"))
            if conf.get("tool", {}).get("commissure-dev", {}).get("solution"):
                return folder
    raise UsageError(
        f"{here} is not inside a wrapper (no pyproject.toml with [tool.commissure-dev])"
    )


def read_pin(wrapper: Path) -> tuple[str, str, str]:
    """(solution name, core URL, rev) recorded in the wrapper's pyproject.toml."""
    conf = tomllib.loads((wrapper / "pyproject.toml").read_text(encoding="utf-8"))
    source = conf["tool"]["uv"]["sources"]["commissure"]
    return conf["tool"]["commissure-dev"]["solution"], source["git"], source["rev"]


# --- commands ------------------------------------------------------------------------------


def _staged(dest: Path, build: Callable[[Path], None]) -> None:
    """Build a new wrapper in a hidden sibling folder, then move it to `dest`.

    Nothing appears at `dest` unless every step succeeded; a failure removes the staging
    folder, which only this call wrote to.
    """
    dest = dest.resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise UsageError(f"{dest} already exists and is not empty")
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        build(staging)
    except BaseException:
        _rmtree(staging)
        raise
    if dest.exists():
        dest.rmdir()
    staging.rename(dest)


def _name_from_remote(remote: str) -> str:
    tail = remote.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail.removesuffix(".git")


def clone(
    remote: str,
    dest: Path,
    *,
    rev: str | None = None,
    core_url: str | None = None,
    name: str | None = None,
    do_sync: bool = True,
) -> Path:
    """Clone a solution into `<dest>/solutions/<name>/` and generate the wrapper around it."""
    name = _check_name(name or _name_from_remote(remote))
    core_url, rev = resolve_pin(rev, core_url)

    def build(stage: Path) -> None:
        git_dir = core_git(stage, core_url)
        _require_commit(git_dir, rev, core_url)
        _run("git", "clone", "-q", remote, stage / "solutions" / name)
        snapshot(stage, git_dir, rev)
        write_generated(stage, name, rev, core_url)

    _staged(dest, build)
    dest = dest.resolve()
    if do_sync:
        sync(dest)
    return dest


def remote_is_empty(url: str) -> bool:
    out = subprocess.run(["git", "ls-remote", url], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise UsageError(f"can't reach {url}: {out.stderr.strip()}")
    return not out.stdout.strip()


def new(
    dest: Path,
    name: str,
    *,
    rev: str | None = None,
    core_url: str | None = None,
    remote: str | None = None,
    do_sync: bool = True,
) -> Path:
    """Scaffold a new solution with the running commissure's template, commit it, and wrap it.

    With `remote` (which must be empty), push that first commit, so the solution never
    exists only on one disk. A remote with commits is refused: the solution exists, so
    `clone` is the command.
    """
    from commissure.scaffold import scaffold_solution  # the template, not runtime code

    name = _check_name(name)
    core_url, rev = resolve_pin(rev, core_url)
    if remote and not remote_is_empty(remote):
        raise UsageError(f"{remote} already has commits; use `commissure dev clone`")

    def build(stage: Path) -> None:
        git_dir = core_git(stage, core_url)
        _require_commit(git_dir, rev, core_url)
        sol = stage / "solutions" / name
        scaffold_solution(sol, name)
        _git(sol, "init", "-q", "-b", "main")
        _git(sol, "add", "-A")
        _git(sol, "commit", "-q", "-m", "Initial commit")
        snapshot(stage, git_dir, rev)
        write_generated(stage, name, rev, core_url)
        # Last, because it is the one step another machine sees.
        if remote:
            _git(sol, "remote", "add", "origin", remote)
            _git(sol, "push", "-q", "-u", "origin", "main")

    _staged(dest, build)
    dest = dest.resolve()
    if do_sync:
        sync(dest)
    return dest


def refresh(wrapper: Path, *, do_sync: bool = True) -> None:
    """Rewrite the generated files from this commissure's templates, keeping the pin.

    Never touches `solutions/` or the `.local` files. The snapshot is rebuilt only when it is
    missing or names another revision.
    """
    name, core_url, rev = read_pin(wrapper)
    if pinned_revision(wrapper) != rev:
        git_dir = core_git(wrapper, core_url)
        _require_commit(git_dir, rev, core_url)
        snapshot(wrapper, git_dir, rev)
    write_generated(wrapper, name, rev, core_url)
    if do_sync:
        sync(wrapper)


def changelog_added(git_dir: Path, old: str, new: str) -> list[str]:
    """Lines of CHANGELOG.md added between two commits, in file order (blank lines kept)."""
    diff = _git(git_dir, "diff", "--unified=0", "--no-color", "--no-ext-diff", old, new, "--",
                "CHANGELOG.md")  # fmt: skip
    added, in_hunk = [], False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("diff --git"):
            in_hunk = False
    return added


def repin(
    wrapper: Path,
    sha: str,
    *,
    do_sync: bool = True,
    run_tests: bool = True,
    echo: Callable[[str], None] = print,
) -> Repin:
    """Move the wrapper's core pin to `sha`.

    Order matters: the sha is checked (full, and a commit at the core URL) before anything is
    written; a failed lock or sync puts `pyproject.toml` and `uv.lock` back; the snapshot is
    replaced only after the environment moved. Failing tests leave the new pin in place:
    fixing them is the next step.
    """
    _check_sha(sha)
    name, core_url, old = read_pin(wrapper)
    git_dir = core_git(wrapper, core_url)
    _require_commit(git_dir, sha, core_url)

    py = wrapper / "pyproject.toml"
    lock = wrapper / "uv.lock"
    saved_py = py.read_text(encoding="utf-8")
    saved_lock = lock.read_bytes() if lock.exists() else None
    pin_line = re.compile(r'^(commissure = \{ git = "[^"]*", rev = ")([0-9a-f]{40})(" \})$', re.M)
    if len(pin_line.findall(saved_py)) != 1:
        raise WrapperError(f"{py} has no single generated pin line; run `commissure dev refresh`")
    py.write_text(pin_line.sub(rf"\g<1>{sha}\g<3>", saved_py), encoding="utf-8")
    if do_sync:
        try:
            sync(wrapper, upgrade=True)
        except WrapperError:
            py.write_text(saved_py, encoding="utf-8")
            if saved_lock is None:
                lock.unlink(missing_ok=True)
            else:
                lock.write_bytes(saved_lock)
            restore_env(wrapper)
            raise
    snapshot(wrapper, git_dir, sha)

    added = changelog_added(git_dir, old, sha) if old != sha else []
    if old == sha:
        echo(f"== already pinned to {sha}")
    elif added:
        echo(f"== core CHANGELOG.md, added since {old}")
        for line in added:
            echo(line)
    else:
        echo(f"== core CHANGELOG.md: nothing added since {old}")

    if do_sync:
        # The new pin's own templates, not this (old) code's: refresh runs from the new install.
        out = subprocess.run(["uv", "run", "commissure", "dev", "refresh"], cwd=wrapper,
                             capture_output=True, text=True, check=False)  # fmt: skip
        if out.returncode != 0:
            echo(f"== generated files not refreshed (`uv run commissure dev refresh`): "
                 f"{(out.stderr or out.stdout).strip().splitlines()[-1:]}")  # fmt: skip
    tests = None
    if run_tests:
        if (wrapper / "solutions" / name / "tests").is_dir():
            tests = subprocess.run(
                ["uv", "run", "pytest", "-q"], cwd=wrapper, check=False
            ).returncode
        else:
            echo(f"no solutions/{name}/tests yet: tests skipped")
    return Repin(old=old, new=sha, added=added, tests=tests)
