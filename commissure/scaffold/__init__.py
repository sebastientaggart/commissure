"""Scaffold: create a solution directory from the built-in template.

A solution is a directory convention (`AGENTS.md`, Vocabulary): a
`recipe/` folder of code, a `workspace/` of data that commissure creates on the first run,
`docs/` for documentation, `intake/` for input documents, and a README. The template lives
inside the package so `commissure init` works wherever commissure is installed.

The template is copied verbatim except for two tokens, `__SOLUTION_NAME__` and `__DATE__`,
and one rename: `_gitignore` is written as `.gitignore` so the template's own ignore rules
never apply inside this package. Existing files are never overwritten.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

TEMPLATE = Path(__file__).parent / "template"
RENAMES = {"_gitignore": ".gitignore"}
TOKENS = ("__SOLUTION_NAME__", "__DATE__")


class ScaffoldError(Exception):
    """The target cannot be scaffolded. Raised before anything is written."""


def template_files() -> list[Path]:
    return sorted(p for p in TEMPLATE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def _target_for(source: Path, target: Path) -> Path:
    rel = source.relative_to(TEMPLATE)
    return target.joinpath(*rel.parts[:-1], RENAMES.get(rel.name, rel.name))


def scaffold_solution(target: Path, name: str | None = None) -> list[Path]:
    """Write the template into `target`, returning the files written (relative to it).

    `target` may already exist; it may even hold other files (an operator often creates
    the folder first). A file the template would write must not exist yet, and `name`
    must be usable as a folder name.
    """
    target = target.resolve()
    name = name or target.name
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ScaffoldError(f"solution name {name!r} is not a usable folder name")
    if target.exists() and not target.is_dir():
        raise ScaffoldError(f"{target} exists and is not a directory")
    sources = template_files()
    if not sources:
        raise ScaffoldError(f"template folder {TEMPLATE} is empty")
    clashes = [t for t in (_target_for(s, target) for s in sources) if t.exists()]
    if clashes:
        shown = ", ".join(str(c.relative_to(target)) for c in clashes[:5])
        raise ScaffoldError(f"{target} already has scaffold files ({shown}); nothing written")
    subs = {"__SOLUTION_NAME__": name, "__DATE__": date.today().isoformat()}
    written = []
    for src in sources:
        text = src.read_text(encoding="utf-8")
        for token, value in subs.items():
            text = text.replace(token, value)
        dest = _target_for(src, target)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        written.append(dest.relative_to(target))
    return written
