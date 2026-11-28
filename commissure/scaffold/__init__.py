"""Scaffold: a solution project from a built-in template, and the agent kit inside it.

A solution is a standalone project (`AGENTS.md`, Vocabulary): its own `pyproject.toml`
depending on commissure, a `recipe/` folder of code, `tests/`, `docs/`, `intake/` for input
documents, and a `workspace/` that commissure creates on the first run. The templates live
inside the package so `commissure new` works wherever commissure is installed, `uvx` included.

Two templates: `project/` (an empty recipe that fails at startup until it has stages) and
`example/` (a working recipe over a small synthetic corpus). Each is copied verbatim except
for the tokens in `TOKENS` and the names in `RENAMES`: the `_` prefix keeps the template's
own ignore rules, project file, and agent instructions from applying inside this package.

The agent kit (`commissure/kit/`: core's rules for recipe authors, its docs, its skills) is
copied to `.agents/commissure/` with `.claude/skills/<skill>` links into it. It is core's,
tracked in the solution's git so an upgrade shows as a diff, and `commissure agent-kit`
replaces it from the installed version. Nothing else in the solution is ever rewritten.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import date
from importlib.metadata import version
from pathlib import Path

from packaging.version import Version

HERE = Path(__file__).parent
TEMPLATES = {"project": HERE / "project", "example": HERE / "example"}
KIT = HERE.parent / "kit"
KIT_DIR = Path(".agents") / "commissure"
SKILLS_DIR = Path(".claude") / "skills"
# A link is the kit's when it points here; any other skill in .claude/skills/ is the solution's.
SKILL_LINK = "../../.agents/commissure/skills/"
RENAMES = {
    "_gitignore": ".gitignore",
    "_pyproject.toml": "pyproject.toml",
    "_AGENTS.md": "AGENTS.md",
    "_CLAUDE.md": "CLAUDE.md",
}
TOKENS = ("__SOLUTION_NAME__", "__DATE__", "__COMMISSURE_REQUIREMENT__", "__DESCRIPTION__")
DESCRIPTIONS = {
    "project": "TODO: one line on what this solution does.",
    "example": "Commissure example: positions and topics in public comments, with evidence.",
}
# The example is the project template with its own recipe and tests: nothing of these is kept.
EXAMPLE_REPLACES = ("recipe", "tests")
SKIP = ("__pycache__", ".DS_Store")
# PEP 508 project names: the solution's name is its pyproject name.
NAME = re.compile(r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")


class ScaffoldError(Exception):
    """The target cannot be scaffolded. Raised before anything is written."""


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not any(s in p.parts for s in SKIP))


def _out(rel: Path) -> str:
    return rel.parent.joinpath(RENAMES.get(rel.name, rel.name)).as_posix()


def template_files(kind: str = "project") -> dict[str, Path]:
    """What `new` writes for `kind`: each output path (relative) and the file it comes from."""
    root = TEMPLATES["project"]
    files = {_out(p.relative_to(root)): p for p in _files(root)}
    if kind == "example":
        files = {k: v for k, v in files.items() if k.split("/")[0] not in EXAMPLE_REPLACES}
        root = TEMPLATES["example"]
        files |= {_out(p.relative_to(root)): p for p in _files(root)}
    return dict(sorted(files.items()))


def requirement() -> str:
    """`commissure~=X.Y.Z` for the running version: patch upgrades only, since a 0.x minor
    release may break recipes (its CHANGELOG says how under **Upgrading**)."""
    return f"commissure~={Version(version('commissure')).base_version}"


def scaffold_solution(target: Path, name: str | None = None, kind: str = "project") -> list[Path]:
    """Write a template and the agent kit into `target`; return the files written (relative).

    `target` may already exist and hold other files (an operator often creates the folder
    and drops documents in first). A file the template or kit would write must not exist
    yet, and `name` must be a valid project name.
    """
    target = target.resolve()
    name = name or target.name
    if not NAME.match(name):
        raise ScaffoldError(
            f"solution name {name!r} must be letters, digits, '.', '_', or '-' "
            "(starting and ending with a letter or digit)"
        )
    if target.exists() and not target.is_dir():
        raise ScaffoldError(f"{target} exists and is not a directory")
    if kind not in TEMPLATES:
        raise ScaffoldError(f"no template {kind!r}")
    sources = template_files(kind)
    planned = [target / rel for rel in sources]
    planned += [target / KIT_DIR, *(target / SKILLS_DIR / s for s in kit_skills())]
    clashes = [t for t in planned if t.exists() or t.is_symlink()]
    if clashes:
        shown = ", ".join(str(c.relative_to(target)) for c in clashes[:5])
        raise ScaffoldError(f"{target} already has scaffold files ({shown}); nothing written")
    subs = {
        "__SOLUTION_NAME__": name,
        "__DATE__": date.today().isoformat(),
        "__COMMISSURE_REQUIREMENT__": requirement(),
        "__DESCRIPTION__": DESCRIPTIONS[kind],
    }
    written = []
    for rel, src in sources.items():
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Input documents (the example's corpus, in intake/<batch>/) are originals: copied
        # byte for byte, never token-substituted. intake/README.md is a template file.
        parts = rel.split("/")
        try:
            original = parts[0] == "intake" and len(parts) > 2
            text = None if original else src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            shutil.copyfile(src, dest)
        else:
            for token, value in subs.items():
                text = text.replace(token, value)
            dest.write_text(text, encoding="utf-8")
        written.append(dest.relative_to(target))
    return written + write_kit(target)


def kit_skills() -> list[str]:
    return sorted(p.name for p in (KIT / "skills").iterdir() if (p / "SKILL.md").is_file())


def _kit_links(project: Path) -> list[Path]:
    skills = project / SKILLS_DIR
    if not skills.is_dir():
        return []
    return [p for p in skills.iterdir() if p.is_symlink() and os.readlink(p).startswith(SKILL_LINK)]


def write_kit(project: Path) -> list[Path]:
    """Replace `.agents/commissure/` with the installed kit and link its skills.

    Only the kit folder and kit links are touched: a skill of the solution's own in
    `.claude/skills/` stays, and one with a kit skill's name is refused before any write.
    """
    project = project.resolve()
    ours = {p.name for p in _kit_links(project)}
    blocked = [
        s
        for s in kit_skills()
        if s not in ours
        and ((project / SKILLS_DIR / s).exists() or (project / SKILLS_DIR / s).is_symlink())
    ]
    if blocked:
        raise ScaffoldError(
            f"{SKILLS_DIR}/ already has a skill of its own named {', '.join(blocked)}; "
            "rename it, then rerun"
        )
    dest = project / KIT_DIR
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".commissure.", dir=dest.parent))
    try:
        for src in _files(KIT):
            out = staging / src.relative_to(KIT)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out)
        (staging / "VERSION").write_text(version("commissure") + "\n", encoding="utf-8")
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    for link in _kit_links(project):
        link.unlink()
    (project / SKILLS_DIR).mkdir(parents=True, exist_ok=True)
    for skill in kit_skills():
        (project / SKILLS_DIR / skill).symlink_to(SKILL_LINK + skill)
    files = [p.relative_to(project) for p in _files(dest)]
    return files + [SKILLS_DIR / s for s in kit_skills()]
