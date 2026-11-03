"""Build and publish client releases: core commissure plus one solution for a target.

    commissure release build <target>              # build/releases/<target>/
    commissure release publish <target> --tarball  # solutions/<s>/releases/*.tar.gz
    commissure release publish <target> --push     # the target's client remote

A target (`solutions/<solution>/releases/<target>.toml`) names a client destination for its
enclosing solution. The solution says what it ships in `solutions/<solution>/release.toml`:
its version and an allowlist of paths (by default `recipe/` and `CHANGELOG.md`). Tests,
fixtures, intake, workspaces, releases, and internal docs never ship because they are never
listed.

The release repo is generated, not developed in: every build replaces its tree with the
allowlisted files, a pinned commissure wheel in `vendor/`, and a generated `pyproject.toml`,
`README.md`, and `release.json`, then commits and tags `v<version>`. Its history is one commit
per release, so a client's `git log` reads like a changelog. A build refuses when the version
was already released, when the solution's files changed without a version bump, or when the
new version has no CHANGELOG entry. Before committing, every shipped file (and every file
inside the vendored wheel) is scanned for built-in credential/path patterns, optional
organization-wide patterns in `solutions/.forbid.toml`, the names of any other solutions in
`solutions/`, and any target-specific `forbid` patterns. Publishing is a separate command, and
pushing needs `--push`.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INCLUDE = ("recipe", "CHANGELOG.md")
ALWAYS_SKIP = ("__pycache__", "*.pyc", ".DS_Store", ".pytest_cache", ".ruff_cache")
GENERATED = ("README.md", "pyproject.toml", "uv.lock", "release.json", ".gitignore", "vendor")
DEFAULT_FORBID = (
    r"/Users/[A-Za-z]|/home/[a-z]",  # a path from a developer machine
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b(?:ghp|gho|github_pat|glpat)_[A-Za-z0-9_]{20,}",  # GitHub / GitLab tokens
    r"\bsk-[A-Za-z0-9]{20,}",  # API keys in the sk- style
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z.]{2,}",  # email addresses
)
RELEASE_GITIGNORE = """\
.venv/
__pycache__/
*.py[cod]
# Client data stays on the client's machine.
solutions/*/intake/*
!solutions/*/intake/README.md
solutions/*/workspace/
.env
"""
INTAKE_README = """\
# Input documents

Put the documents to process here, one folder per batch (`intake/<batch>/`). This folder
is not tracked: client data stays on this machine. See the solution's section of the
top-level README for the commands that read it.
"""


class ReleaseError(Exception):
    """A release that must not be built or published as asked."""


@dataclass
class Solution:
    name: str
    version: str
    summary: str
    usage: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    extras: tuple[str, ...]
    files: dict[str, Path] = field(default_factory=dict)  # release path -> source file

    def digest(self) -> str:
        h = hashlib.sha256()
        for rel, src in sorted(self.files.items()):
            h.update(rel.encode() + b"\0" + hashlib.sha256(src.read_bytes()).digest())
        return h.hexdigest()


@dataclass
class Target:
    name: str
    title: str
    solution: str
    remote: str | None
    tarball: bool
    forbid: tuple[str, ...] = ()  # regexes no shipped file may match


def resolve_root(cwd: Path | None = None) -> tuple[Path, str | None]:
    """Return `(repo_root, solution)` from `cwd` (supports running inside `solutions/<name>`)."""
    here = (cwd or Path.cwd()).resolve()
    if (here / "release.toml").is_file() and here.parent.name == "solutions":
        return here.parents[1], here.name
    if (here / "solutions").is_dir() or (here / "pyproject.toml").is_file():
        return here, None
    return ROOT, None


def _solution_patterns(name: str) -> list[str]:
    """Regexes matching a solution's folder name and any multi-word slug after a client prefix."""
    candidates = [name]
    if "-" in name:
        _, rest = name.split("-", 1)
        if "-" in rest or "_" in rest:
            candidates.append(rest)
    patterns = []
    for item in candidates:
        parts = [re.escape(p) for p in re.split(r"[-_]+", item) if p]
        if parts:
            patterns.append(r"(?i)\b" + r"[ _-]?".join(parts) + r"\b")
    return patterns


def _find_target(root: Path, spec: str) -> tuple[str, str, Path]:
    sdir = root / "solutions"
    if "/" in spec:
        solution, name = spec.split("/", 1)
        path = sdir / solution / "releases" / f"{name}.toml"
        if not path.is_file():
            raise ReleaseError(f"no target {spec}: {path} is missing")
        return solution, name, path
    matches = (
        sorted(
            p
            for p in sdir.glob(f"*/releases/{spec}.toml")
            if p.is_file() and not p.parents[1].name.startswith(".")
        )
        if sdir.is_dir()
        else []
    )
    if not matches:
        raise ReleaseError(f"no target {spec}: no solutions/*/releases/{spec}.toml")
    if len(matches) > 1:
        found = ", ".join(f"{p.parents[1].name}/{spec}" for p in matches)
        raise ReleaseError(
            f"target {spec} exists in multiple solutions ({found}); pass <solution>/{spec}"
        )
    path = matches[0]
    return path.parents[1].name, spec, path


def load_target(root: Path, spec: str) -> Target:
    solution, name, path = _find_target(root, spec)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if "solutions" in data and tuple(data.get("solutions") or ()) != (solution,):
        raise ReleaseError(
            f"{path}: a target in solutions/{solution}/releases/ may only package {solution}"
        )
    sdir = root / "solutions"
    local_forbid_path = sdir / ".forbid.toml"
    local_shared = (
        tomllib.loads(local_forbid_path.read_text(encoding="utf-8"))
        if local_forbid_path.is_file()
        else {}
    )
    others = (
        sorted(
            d.name
            for d in sdir.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name != solution
        )
        if sdir.is_dir()
        else []
    )
    allowed = set(_solution_patterns(solution))
    other_forbid = [p for o in others for p in _solution_patterns(o) if p not in allowed]
    forbid = (
        *DEFAULT_FORBID,
        *(p for p in local_shared.get("forbid", ()) if p not in allowed),
        *other_forbid,
        *data.get("forbid", ()),
    )
    for pattern in forbid:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ReleaseError(f"forbid pattern {pattern!r} is not a regex: {exc}") from None
    return Target(
        name=name,
        title=data.get("title", name),
        solution=solution,
        remote=data.get("remote") or None,
        tarball=bool(data.get("tarball", True)),
        forbid=forbid,
    )


def _texts(path: Path):
    """(name, text) for a shipped file; a wheel is read member by member."""
    if path.suffix == ".whl":
        try:
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    yield f"{path.name}:{member}", z.read(member).decode("utf-8", "ignore")
        except zipfile.BadZipFile:
            yield path.name, path.read_bytes().decode("utf-8", "ignore")
        return
    yield path.name, path.read_bytes().decode("utf-8", "ignore")


def scan_forbidden(dest: Path, patterns: tuple[str, ...]) -> list[str]:
    """Every match of a forbidden pattern in the release tree: `file:line: text`."""
    compiled = [re.compile(p) for p in patterns]
    hits = []
    for path in sorted(p for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts):
        rel = path.relative_to(dest).as_posix()
        for name, text in _texts(path):
            member = rel if name == path.name else name.split(":", 1)[1]
            label = rel if name == path.name else f"{rel}!{member}"
            if _skipped(member, ()):
                hits.append(f"{label}: forbidden file in release ({PurePosixPath(member).name})")
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                for rx in compiled:
                    if rx.search(line):
                        hits.append(f"{label}:{number}: {rx.pattern} in {line.strip()[:120]!r}")
    return hits


def _skipped(rel: str, exclude: tuple[str, ...]) -> bool:
    parts = rel.split("/")
    if any(fnmatch.fnmatch(p, pat) for p in parts for pat in ALWAYS_SKIP):
        return True
    return any(fnmatch.fnmatch(rel, pat) for pat in exclude)


def load_solution(root: Path, name: str) -> Solution:
    sdir = root / "solutions" / name
    path = sdir / "release.toml"
    if not path.is_file():
        raise ReleaseError(f"solution {name} has no release.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for key in ("version", "summary", "usage"):
        if not data.get(key):
            raise ReleaseError(f"{path}: `{key}` is required")
        if "TODO" in str(data[key]):
            raise ReleaseError(f"{path}: `{key}` still says TODO; write it before releasing")
    sol = Solution(
        name=name,
        version=str(data["version"]),
        summary=data["summary"],
        usage=data["usage"].strip(),
        include=tuple(data.get("include") or DEFAULT_INCLUDE),
        exclude=tuple(data.get("exclude") or ()),
        extras=tuple(data.get("extras") or ()),
    )
    for entry in sol.include:
        src = sdir / entry
        if not src.exists():
            raise ReleaseError(f"{path}: include `{entry}` does not exist")
        sources = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
        for f in sources:
            rel = f.relative_to(sdir).as_posix()
            if not _skipped(rel, sol.exclude):
                sol.files[f"solutions/{name}/{rel}"] = f
    changelog = sdir / "CHANGELOG.md"
    if not changelog.is_file() or f"## {sol.version}" not in changelog.read_text(encoding="utf-8"):
        raise ReleaseError(f"{name} {sol.version} has no `## {sol.version}` entry in CHANGELOG.md")
    return sol


def git(cwd: Path, *args: str, sign: bool = True) -> str:
    config = [] if sign else ["-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
    result = subprocess.run(
        ["git", *config, *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def build_wheel(root: Path, out: Path) -> Path:
    shutil.rmtree(out, ignore_errors=True)  # a stale wheel of another version would ship
    out.mkdir(parents=True)
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=root, check=True)
    wheels = sorted(out.glob("commissure-*-py3-none-any.whl"))
    if len(wheels) != 1:
        raise ReleaseError(f"expected one commissure wheel in {out}, found {len(wheels)}")
    return wheels[0]


def core_version(wheel: Path) -> str:
    return wheel.name.split("-")[1]


def _readme(target: Target, sol: Solution, core: str) -> str:
    return f"""# {target.title}

Release v{sol.version}, built {date.today().isoformat()} on commissure {core}.

| Solution | Version | What it does |
| --- | --- | --- |
| `{sol.name}` | {sol.version} | {sol.summary} |

## Setup

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). From this folder:

```sh
uv sync    # installs commissure {core} from vendor/ and its dependencies from PyPI
```

Scanned or image-only PDFs need the Tesseract binary (`brew install tesseract`, or
`apt install tesseract-ocr`). Pin it (`brew pin tesseract`): its version is part of the OCR
parser's version, so an upgrade makes the next OCR run recognise every document again.

Each solution keeps its inputs in `solutions/<name>/intake/` and its data in
`solutions/<name>/workspace/`; neither is tracked. Commands run from the solution's folder.

## {sol.name}

{sol.usage}

## Changes

`solutions/{sol.name}/CHANGELOG.md` lists what changed in each version.
"""


def _pyproject(target: Target, sol: Solution, wheel: Path) -> str:
    extras = sorted(set(sol.extras))
    spec = f"commissure[{','.join(extras)}]" if extras else "commissure"
    return f"""[project]
name = "{target.name}"
version = "{sol.version}"
description = "{target.title}"
requires-python = ">=3.12"
dependencies = ["{spec}=={core_version(wheel)}"]

[tool.uv]
package = false

[tool.uv.sources]
commissure = {{ path = "vendor/{wheel.name}" }}
"""


def _previous(dest: Path) -> dict | None:
    if not (dest / ".git").exists():
        return None
    try:
        return json.loads(git(dest, "show", "HEAD:release.json"))
    except ReleaseError:
        return None


def _has_tag(repo: Path, tag: str) -> bool:
    try:
        return tag in git(repo, "tag", "--list", tag).split()
    except ReleaseError:  # not a git repository (tests)
        return False


def _check_versions(previous: dict | None, sol: Solution, sdir: Path, target_name: str) -> None:
    if previous is not None:
        old = previous.get("solutions", {}).get(sol.name)
        if old is not None and old["version"] == sol.version:
            if old["digest"] != sol.digest():
                raise ReleaseError(
                    f"{sol.name} changed since release v{sol.version} but is still version"
                    f" {sol.version}: bump `version` in its release.toml and add a CHANGELOG entry"
                )
            raise ReleaseError(f"nothing changed since release v{sol.version}")
    tag = f"release/{target_name}/v{sol.version}"
    if _has_tag(sdir, tag):
        raise ReleaseError(
            f"{target_name} v{sol.version} is already tagged ({tag});"
            " bump `version` in release.toml and add a CHANGELOG entry"
        )


def build(
    root: Path,
    name: str,
    *,
    dest: Path | None = None,
    wheel: Path | None = None,
    run_tests: bool = True,
    lock: bool = True,
    offline: bool = False,
    smoke: bool = True,
    sign: bool = True,
    allow_dirty: bool = False,
    tag_monorepo: bool = True,
) -> tuple[Path, str]:
    target = load_target(root, name)
    sol = load_solution(root, target.solution)
    sdir = root / "solutions" / sol.name
    if not allow_dirty and git(sdir, "status", "--porcelain"):
        raise ReleaseError(f"solutions/{sol.name} has uncommitted changes; commit them first")
    if run_tests:
        paths = [p for p in ("tests", f"solutions/{sol.name}/tests") if (root / p).is_dir()]
        if subprocess.run(["uv", "run", "pytest", "-q", *paths], cwd=root).returncode != 0:
            raise ReleaseError("tests failed; nothing was released")
    dest = dest or root / "build" / "releases" / target.name
    if not dest.exists():
        if target.remote:
            subprocess.run(["git", "clone", target.remote, str(dest)], check=True)
            git(dest, "checkout", "-q", "-B", "main", sign=sign)  # an empty remote has no branch
        else:
            dest.mkdir(parents=True)
            git(dest, "init", "-q", "-b", "main", sign=sign)
    wheel = wheel or build_wheel(root, root / "build" / "wheel")
    core = core_version(wheel)
    previous = _previous(dest)
    _check_versions(previous, sol, sdir, target.name)
    version = sol.version

    for child in dest.iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for rel, src in sol.files.items():
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    intake = dest / "solutions" / sol.name / "intake"
    intake.mkdir(parents=True, exist_ok=True)
    (intake / "README.md").write_text(INTAKE_README, encoding="utf-8")
    (dest / "vendor").mkdir(exist_ok=True)
    shutil.copy2(wheel, dest / "vendor" / wheel.name)
    (dest / ".gitignore").write_text(RELEASE_GITIGNORE, encoding="utf-8")
    (dest / "README.md").write_text(_readme(target, sol, core), encoding="utf-8")
    (dest / "pyproject.toml").write_text(_pyproject(target, sol, wheel), encoding="utf-8")
    manifest = {
        "release": version,
        "target": target.name,
        "date": date.today().isoformat(),
        "commissure": core,
        "solutions": {sol.name: {"version": sol.version, "digest": sol.digest()}},
    }
    (dest / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    hits = scan_forbidden(dest, target.forbid)
    if hits:
        shown = "\n  ".join(hits[:20]) + (
            f"\n  ... {len(hits) - 20} more" if len(hits) > 20 else ""
        )
        raise ReleaseError(
            f"{len(hits)} forbidden match(es) in the release; nothing was committed and the tree"
            f" is left in {dest} for inspection:\n  {shown}"
        )
    if lock:
        cmd = ["uv", "lock", *(["--offline"] if offline else [])]
        subprocess.run(cmd, cwd=dest, check=True)
    if smoke:
        _smoke(dest, sol)

    git(dest, "add", "-A", sign=sign)
    git(
        dest,
        "commit",
        "-q",
        "-m",
        f"Release v{version}: {sol.name} {sol.version} (commissure {core})",
        sign=sign,
    )
    git(dest, "tag", "-a", f"v{version}", "-m", f"Release v{version}", sign=sign)
    if tag_monorepo:
        tag = f"release/{target.name}/v{version}"
        git(
            sdir,
            "tag",
            "-a",
            tag,
            "-m",
            f"{target.name} v{version}: {sol.name} {sol.version}",
            sign=sign,
        )
    return dest, version


def _smoke(dest: Path, sol: Solution) -> None:
    """Install from the vendored wheel and load the shipped recipe, as the client would."""
    subprocess.run(["uv", "sync", "--frozen", "--quiet"], cwd=dest, check=True)
    recipe = dest / "solutions" / sol.name / "recipe"
    code = (
        "import sys; from commissure.recipes import load_recipe;"
        f" r = load_recipe(__import__('pathlib').Path({str(recipe)!r}));"
        " print(r.name, sorted(r.stages))"
    )
    result = subprocess.run(
        ["uv", "run", "--frozen", "python", "-B", "-c", code],
        cwd=dest, capture_output=True, text=True,
    )  # fmt: skip
    if result.returncode != 0:
        raise ReleaseError(f"{sol.name} does not load from the release: {result.stderr[-800:]}")
    shutil.rmtree(dest / ".venv", ignore_errors=True)


def publish(
    root: Path, name: str, *, tarball: bool, push: bool, dest: Path | None = None
) -> list[str]:
    target = load_target(root, name)
    dest = dest or root / "build" / "releases" / target.name
    previous = _previous(dest)
    if previous is None:
        raise ReleaseError(f"no release of {target.name} has been built")
    tag = f"v{previous['release']}"
    done = []
    if tarball:
        out = root / "solutions" / target.solution / "releases" / f"{target.name}-{tag}.tar.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        git(dest, "archive", "--format=tar.gz", f"--prefix={target.name}-{tag}/",
            "-o", str(out), tag)  # fmt: skip
        done.append(str(out))
    if push:
        if not target.remote:
            raise ReleaseError(f"target {target.name} has no remote to push to")
        git(dest, "push", target.remote, "main", "--follow-tags")
        done.append(f"pushed {tag} to {target.remote}")
    if not done:
        raise ReleaseError("nothing to publish: pass --tarball and/or --push")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build the next release of a target")
    b.add_argument("target")
    b.add_argument("--skip-tests", action="store_true")
    b.add_argument("--offline", action="store_true", help="lock from the uv cache only")
    b.add_argument("--allow-dirty", action="store_true", help="release uncommitted changes")
    p = sub.add_parser("publish", help="publish the latest built release")
    p.add_argument("target")
    p.add_argument("--tarball", action="store_true")
    p.add_argument("--push", action="store_true", help="push to the target's remote")
    args = parser.parse_args(argv)
    root, default_sol = resolve_root()
    spec = f"{default_sol}/{args.target}" if default_sol and "/" not in args.target else args.target
    try:
        if args.command == "build":
            dest, version = build(
                root, spec, run_tests=not args.skip_tests, offline=args.offline,
                allow_dirty=args.allow_dirty,
            )  # fmt: skip
            print(f"built {args.target} v{version} in {dest}")
        else:
            for line in publish(root, spec, tarball=args.tarball, push=args.push):
                print(line)
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
