"""Recipes: loading a recipe folder, fingerprinting its stages, and planning a stage.

A recipe is a folder with a `recipe.py` that defines `STAGES`, a list of `Stage`.
Recipe modules import each other relatively (`from .schema import Report`); the folder
is loaded as a package, so absolute imports of siblings fail rather than resolving
somewhere unexpected.

Fingerprints are labels and a resume guard, never a cache key (Records 1-3):
- a stage's fingerprint covers its declaration and the source of every recipe module
  its functions reach through relative imports, transitively, plus the files in
  `Stage.assets`. It does not cover `commissure.patterns`, core, or library versions,
  so a change there is not labeled stale;
- the recipe fingerprint covers every file in the folder (templates, data, prompts),
  so nothing non-Python can change under a resumed run.

Item stages see one current item with its text and upstream payloads. Group and run
stages see a `Group` of complete items plus the items they had to leave out, so an
exclusion is never silent. A group stage places an incomplete item by calling `group_by`
on it with `text=None` and no upstream payloads: a locator-based grouper places it, a
grouper that reads upstream payloads raises and the item stays unplaced.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import inspect
import json
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ValidationError

from commissure.evidence import index as index_evidence
from commissure.evidence import resolve as resolve_evidence
from commissure.limits import Limits
from commissure.llm import validate_stage_llm
from commissure.parse import DEFAULT_FOR, PARSERS, extension
from commissure.records import Diagnostic, Outcome, RecordEnvelope
from commissure.review import Check, client_summary
from commissure.runner import Plan, RunContext, Subject, canonical
from commissure.workspace import Workspace, transaction, utc_now

SUBJECTS = ("item", "group", "run")


class RecipeError(Exception):
    """The recipe cannot be loaded or is inconsistent. Raised before any run starts."""


@dataclass(frozen=True)
class Stage:
    name: str
    fn: Callable[..., Any]
    schema: type[BaseModel]
    subject: str = "item"
    needs: tuple[str, ...] = ()  # earlier item or (for group/run stages) group stages
    text: str | tuple[str, ...] | None = None  # parser name(s) in order of preference;
    # "default" means each item's default parser. ("ocr", "default") takes an item's ocr
    # row when one exists and its default parser's row otherwise.
    roles: tuple[str, ...] | None = None  # item roles in scope; None means all
    group_by: Callable[[Item], str | None] | None = None  # group stages only
    params: Mapping[str, Any] = field(default_factory=dict)
    assets: tuple[str, ...] = ()  # recipe-relative files hashed into stage_fp
    allow_empty: bool = False
    allow_nothing_attempted: bool = False
    fail_if_all_quarantined: bool = True
    gate: Callable[[dict[str, int]], list[str]] | None = None  # failures; empty passes
    # Review questions over this stage's payload fields. Not in `stage_fingerprint`:
    # asking a question about a result does not change the result.
    checks: tuple[Check, ...] = ()
    # When True, the plan-time review snapshot enters this stage's input fingerprint and
    # `ctx.review_report()` returns it. Default False is omitted from `stage_fingerprint`
    # so existing outputs stay current.
    reads_review: bool = False


@dataclass(frozen=True)
class Source:
    name: str
    uri: str


@dataclass(frozen=True)
class UpstreamOutcome:
    outcome: Outcome
    reason_code: str | None
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class Item:
    key: str
    item_version_id: int
    locator: str
    role: str
    bytes_hash: str
    size_bytes: int
    path: Path  # the ingested bytes (read only)
    source: Source
    text: str | None = None
    structure: dict[str, Any] | None = None
    parser: str | None = None  # the parser whose row supplied `text`
    extracted_text_id: int | None = None  # that row; `Evidence` offsets index into it
    upstream: Mapping[str, BaseModel] = field(default_factory=dict)
    upstream_outcomes: Mapping[str, UpstreamOutcome] = field(default_factory=dict)


@dataclass(frozen=True)
class Excluded:
    key: str
    locator: str
    reason: str


@dataclass(frozen=True)
class Group:
    key: str
    items: tuple[Item, ...]
    excluded: tuple[Excluded, ...] = ()
    upstream: Mapping[str, Mapping[str, BaseModel]] = field(default_factory=dict)
    upstream_outcomes: Mapping[str, Mapping[str, UpstreamOutcome]] = field(default_factory=dict)


@dataclass(frozen=True)
class Interface:
    """An optional interactive local web interface shipped inside a recipe folder."""

    title: str
    render: Callable[..., str]
    subtitle: str = ""
    actions: Mapping[str, Callable[..., str]] = field(default_factory=dict)
    brand_dir: str | bool | None = None
    footer: str | None = None


@dataclass
class Recipe:
    name: str
    path: Path
    stages: dict[str, Stage]
    fingerprint: str
    stage_fps: dict[str, str]
    interface: Interface | None = None
    limits: Limits | None = None

    def stage_name(self, stage: str) -> str:
        return f"{self.name}.{stage}"


def text_parsers(text: str | tuple[str, ...] | None) -> tuple[str, ...]:
    """A stage's `text` as a tuple of parser names, in order of preference."""
    if text is None:
        return ()
    return (text,) if isinstance(text, str) else tuple(text)


def parsers_for(text: str | tuple[str, ...] | None, locator: str) -> list[str]:
    """Parsers that could supply text for `locator`, in preference order, none repeated."""
    ext = extension(locator)
    out: list[str] = []
    for name in text_parsers(text):
        resolved = DEFAULT_FOR.get(ext) if name == "default" else name
        if resolved and ext in PARSERS[resolved].extensions and resolved not in out:
            out.append(resolved)
    return out


# --- loading ----------------------------------------------------------------------


def _hash_files(paths: list[Path], root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def recipe_fingerprint(folder: Path) -> str:
    files = [
        p
        for p in folder.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not any(part.startswith(".") for part in p.relative_to(folder).parts)
    ]
    return _hash_files(files, folder)


def _module_file(folder: Path, base: Path, dotted: str) -> list[Path]:
    target = base.joinpath(*dotted.split(".")) if dotted else base
    found = [p for p in (target.with_suffix(".py"), target / "__init__.py") if p.is_file()]
    return [p for p in found if p.resolve().is_relative_to(folder.resolve())]


def import_closure(folder: Path, roots: list[Path]) -> list[Path]:
    """Recipe files reachable from `roots` through relative imports, transitively."""
    seen: set[Path] = set()
    todo = [r for r in roots if r.resolve().is_relative_to(folder.resolve())]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            base = path.parent
            for _ in range(node.level - 1):
                base = base.parent
            if node.module:
                todo += _module_file(folder, base, node.module)
            for alias in node.names:  # `from . import x` may name a module
                todo += _module_file(
                    folder, base, f"{node.module}.{alias.name}" if node.module else alias.name
                )
    return sorted(seen)


def _source(fn: Any) -> Path | None:
    try:
        return Path(inspect.getfile(fn))
    except TypeError:
        return None


def _inline_from_recipe_py(folder: Path, objs: tuple[Any, ...]) -> tuple[list[str], list[Path]]:
    recipe_py = (folder / "recipe.py").resolve()
    inline = [o for o in objs if o is not None and (p := _source(o)) and p.resolve() == recipe_py]
    if not inline or not recipe_py.is_file():
        return [], []
    src = recipe_py.read_text()
    tree = ast.parse(src, str(recipe_py))
    top_defs: dict[str, ast.AST] = {}
    rel_imports: dict[str, list[Path]] = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_defs[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    top_defs[t.id] = stmt
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            top_defs[stmt.target.id] = stmt
        elif isinstance(stmt, ast.ImportFrom) and stmt.level > 0:
            base = recipe_py.parent
            for _ in range(stmt.level - 1):
                base = base.parent
            mod_files = _module_file(folder, base, stmt.module) if stmt.module else []
            for alias in stmt.names:
                bound = alias.asname or alias.name
                sub = _module_file(
                    folder, base, f"{stmt.module}.{alias.name}" if stmt.module else alias.name
                )
                rel_imports[bound] = mod_files + sub
    todo: list[ast.AST] = []
    for o in inline:
        name = getattr(o, "__name__", None)
        if name and name in top_defs:
            todo.append(top_defs[name])
            continue
        lineno = getattr(getattr(o, "__code__", None), "co_firstlineno", -1)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                and node.lineno == lineno
            ):
                todo.append(node)
    seen_pos: set[tuple[int, int]] = set()
    segments: list[tuple[tuple[int, int], str]] = []
    extra_roots: list[Path] = []
    while todo:
        node = todo.pop()
        pos = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        if pos in seen_pos:
            continue
        seen_pos.add(pos)
        segments.append((pos, ast.get_source_segment(src, node) or ast.unparse(node)))
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id in top_defs:
                    todo.append(top_defs[child.id])
                if child.id in rel_imports:
                    extra_roots.extend(rel_imports[child.id])
    return [seg for _, seg in sorted(segments)], extra_roots


def stage_fingerprint(folder: Path, stage: Stage) -> str:
    recipe_py = (folder / "recipe.py").resolve()
    objs = (stage.fn, stage.schema, stage.group_by, stage.gate)
    roots = [p for p in map(_source, objs) if p and p.resolve() != recipe_py]
    inline_sources, inline_roots = _inline_from_recipe_py(folder, objs)
    roots.extend(inline_roots)
    declaration: dict[str, Any] = {
        "fn": stage.fn.__qualname__,
        "subject": stage.subject,
        "needs": list(stage.needs),
        "text": list(stage.text) if isinstance(stage.text, tuple) else stage.text,
        "roles": list(stage.roles) if stage.roles is not None else None,
        "schema": stage.schema.model_json_schema(),
        "group_by": getattr(stage.group_by, "__qualname__", None),
        "gate": getattr(stage.gate, "__qualname__", None),
        "assets": list(stage.assets),
        "allow_empty": stage.allow_empty,
        "allow_nothing_attempted": stage.allow_nothing_attempted,
        "fail_if_all_quarantined": stage.fail_if_all_quarantined,
    }
    # Default False is omitted so adding the field does not mark every stage code_stale.
    if stage.reads_review:
        declaration["reads_review"] = True
    if inline_sources:
        declaration["inline"] = inline_sources
    files = sorted(
        set(import_closure(folder, roots)) | {(folder / a).resolve() for a in stage.assets}
    )
    h = hashlib.sha256(canonical(declaration).encode())
    h.update(_hash_files(files, folder).encode())
    return h.hexdigest()[:16]


def _check(stages: list[Any], folder: Path) -> dict[str, Stage]:
    if not isinstance(stages, list) or not stages:
        raise RecipeError("recipe.py must define STAGES as a non-empty list of Stage")
    root = folder.resolve()
    by_name: dict[str, Stage] = {}
    for s in stages:
        if not isinstance(s, Stage):
            raise RecipeError(f"STAGES contains {s!r}, not a Stage")
        if s.name in by_name or s.name in ("ingest", "parse") or "." in s.name:
            raise RecipeError(f"stage name {s.name!r} is reserved, repeated, or has a dot")
        if s.subject not in SUBJECTS:
            raise RecipeError(f"stage {s.name}: subject must be one of {SUBJECTS}")
        if (s.subject == "group") != (s.group_by is not None):
            raise RecipeError(f"stage {s.name}: group_by is required for, and only for, groups")
        if not (isinstance(s.schema, type) and issubclass(s.schema, BaseModel)):
            raise RecipeError(f"stage {s.name}: schema must be a pydantic model class")
        if s.schema.model_config.get("extra") != "forbid":
            raise RecipeError(f"stage {s.name}: schema {s.schema.__name__} must set extra='forbid'")
        if s.text is not None and not text_parsers(s.text):
            raise RecipeError(f"stage {s.name}: text must name at least one parser")
        for name in text_parsers(s.text):
            if name != "default" and name not in PARSERS:
                raise RecipeError(f"stage {s.name}: unknown parser {name!r}")
        allowed_need_subjects = ("item",) if s.subject == "item" else ("item", "group")
        for need in s.needs:
            if need not in by_name or by_name[need].subject not in allowed_need_subjects:
                kind_msg = (
                    "an earlier item stage"
                    if s.subject == "item"
                    else "an earlier item or group stage"
                )
                raise RecipeError(f"stage {s.name}: needs {need!r}, which must be {kind_msg}")
        for asset in s.assets:
            if not isinstance(asset, str) or not asset:
                raise RecipeError(f"stage {s.name}: asset paths must be non-empty strings")
            target = (root / asset).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise RecipeError(f"stage {s.name}: missing asset {asset!r} in {root}")
        try:
            canonical(dict(s.params))
        except TypeError as exc:
            raise RecipeError(f"stage {s.name}: params must be JSON-serializable ({exc})") from None
        if not isinstance(s.checks, tuple) or not all(isinstance(c, Check) for c in s.checks):
            raise RecipeError(f"stage {s.name}: checks must be a tuple of Check")
        patterns = [c.pattern for c in s.checks]
        if len(patterns) != len(set(patterns)):
            raise RecipeError(f"stage {s.name}: each check pattern may be declared once")
        by_name[s.name] = s
    return by_name


_LOAD_LOCK = threading.RLock()


def _check_interface(interface: Any, folder: Path) -> Interface | None:
    if interface is None:
        return None
    if not isinstance(interface, Interface):
        raise RecipeError(f"INTERFACE is {interface!r}, not an Interface")
    if not isinstance(interface.title, str) or not interface.title.strip():
        raise RecipeError("INTERFACE.title must be a non-empty string")
    if not isinstance(interface.subtitle, str):
        raise RecipeError("INTERFACE.subtitle must be a string")
    if not callable(interface.render):
        raise RecipeError("INTERFACE.render must be callable")
    if interface.footer is not None and not isinstance(interface.footer, str):
        raise RecipeError("INTERFACE.footer must be a string or None")
    for name, fn in interface.actions.items():
        if not isinstance(name, str) or not name.strip() or "/" in name or "." in name:
            raise RecipeError(f"INTERFACE action name {name!r} must be a non-empty segment")
        if not callable(fn):
            raise RecipeError(f"INTERFACE action {name!r} must be callable")
    if interface.brand_dir is not None and interface.brand_dir is not False:
        if not isinstance(interface.brand_dir, str):
            raise RecipeError("INTERFACE.brand_dir must be a relative path string, False, or None")
        root = folder.resolve()
        target = (root / interface.brand_dir).resolve()
        if not interface.brand_dir or not target.is_relative_to(root) or not target.is_dir():
            raise RecipeError(f"INTERFACE.brand_dir {interface.brand_dir!r} is missing in {root}")
    return interface


def _check_limits(limits_obj: Any) -> Limits | None:
    if limits_obj is None:
        return None
    if not isinstance(limits_obj, Limits):
        raise RecipeError(f"LIMITS is {limits_obj!r}, not a Limits")
    return limits_obj


def _exec_recipe_module(folder: Path) -> Any:
    entry = folder / "recipe.py"
    if not entry.is_file():
        raise RecipeError(f"{folder} has no recipe.py")
    # `.pyc` headers store 32-bit integer-second mtimes + file size; same-second same-size
    # edits (e.g. `v1` -> `v2`) would otherwise execute stale cached bytecode from `__pycache__`.
    for pyc in folder.rglob("__pycache__/*.pyc"):
        with contextlib.suppress(OSError):
            pyc.unlink()
    importlib.invalidate_caches()
    package = f"commissure_recipe_{hashlib.sha256(str(folder).encode()).hexdigest()[:12]}"
    for name in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
        del sys.modules[name]  # always load the code as it is on disk now
    spec = importlib.util.spec_from_file_location(
        package, entry, submodule_search_locations=[str(folder)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RecipeError(f"loading {entry} failed: {type(exc).__name__}: {exc}") from exc
    return module


def load_recipe_limits(path: Path) -> Limits | None:
    """Load optional `LIMITS` from `recipe.py` without requiring `STAGES` to be populated yet."""
    folder = path.resolve()
    with _LOAD_LOCK:
        module = _exec_recipe_module(folder)
        return _check_limits(getattr(module, "LIMITS", None))


def load_recipe(path: Path) -> Recipe:
    folder = path.resolve()
    with _LOAD_LOCK:
        module = _exec_recipe_module(folder)
        stages = _check(getattr(module, "STAGES", None), folder)
        interface = _check_interface(getattr(module, "INTERFACE", None), folder)
        limits = _check_limits(getattr(module, "LIMITS", None))
        return Recipe(
            name=folder.name,
            path=folder,
            stages=stages,
            fingerprint=recipe_fingerprint(folder),
            stage_fps={n: stage_fingerprint(folder, s) for n, s in stages.items()},
            interface=interface,
            limits=limits,
        )


def register_stages(
    conn: sqlite3.Connection,
    recipe: Recipe,
    param_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Record the current fingerprint of every stage, so the views can label outputs."""
    overrides = param_overrides or {}
    now = utc_now()
    with transaction(conn):
        for n, s in recipe.stages.items():
            full = recipe.stage_name(n)
            default_json = canonical(dict(s.params))
            if n in overrides:
                effective_json = canonical(dict(overrides[n]))
                conn.execute(
                    "INSERT INTO stages (stage, stage_fp, params, default_params, recipe_fp,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (stage) DO UPDATE SET"
                    " stage_fp = excluded.stage_fp, params = excluded.params,"
                    " default_params = excluded.default_params, recipe_fp = excluded.recipe_fp,"
                    " updated_at = excluded.updated_at",
                    (
                        full,
                        recipe.stage_fps[n],
                        effective_json,
                        default_json,
                        recipe.fingerprint,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO stages (stage, stage_fp, params, default_params, recipe_fp,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (stage) DO UPDATE SET"
                    " stage_fp = excluded.stage_fp,"
                    " params = CASE WHEN stages.default_params != excluded.default_params"
                    "               THEN excluded.default_params ELSE stages.params END,"
                    " default_params = excluded.default_params, recipe_fp = excluded.recipe_fp,"
                    " updated_at = excluded.updated_at",
                    (
                        full,
                        recipe.stage_fps[n],
                        default_json,
                        default_json,
                        recipe.fingerprint,
                        now,
                    ),
                )


# --- planning ---------------------------------------------------------------------


def _fp(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _items(
    ws: Workspace,
    recipe: Recipe,
    stage: Stage,
    *,
    review_fp: str | None = None,
) -> list[tuple[Item, str, tuple, str | None]]:
    """(item, input fingerprint, consumed outcome ids, reason it is incomplete or None).

    `review_fp` is the hash of the plan-time review snapshot; when set it enters every
    subject's input fingerprint under the key `review`.
    """
    conn = ws.conn
    rows = conn.execute(
        "SELECT item_id, item_version_id, source_uri, locator, role, bytes_hash, size_bytes"
        " FROM current_items ORDER BY item_version_id"
    ).fetchall()
    if stage.roles is not None:
        rows = [r for r in rows if r["role"] in stage.roles]
    item_needs = [n for n in stage.needs if recipe.stages[n].subject == "item"]
    records: dict[str, dict[str, sqlite3.Row]] = {}
    seen: dict[str, set[str]] = {}
    for need in item_needs:
        full = recipe.stage_name(need)
        records[need] = {
            r["subject_key"]: r
            for r in conn.execute("SELECT * FROM current_records WHERE stage = ?", (full,))
        }
        seen[need] = {
            r[0]
            for r in conn.execute(
                "SELECT subject_key FROM current_outcomes WHERE stage = ?", (full,)
            )
        }

    out = []
    for r in rows:
        key = f"item:{r['item_id']}"
        reason, consumed, text_row = None, [], None
        if stage.text is not None:
            # The first preferred parser with a fresh row wins; failing that, the first
            # with any row (reported as text_stale); failing that, no_text.
            found = [
                row
                for parser in parsers_for(stage.text, r["locator"])
                if (
                    row := conn.execute(
                        "SELECT ct.*, (SELECT max(o.id) FROM outcomes o"
                        "  WHERE o.extracted_text_id = ct.extracted_text_id) AS parse_outcome_id"
                        " FROM current_text ct WHERE ct.item_version_id = ? AND ct.parser = ?",
                        (r["item_version_id"], parser),
                    ).fetchone()
                )
            ]
            text_row = next((row for row in found if not row["stale"]), found[0] if found else None)
            if not text_row:
                reason = "no_text"
            elif text_row["stale"]:
                reason = "text_stale"
            else:
                consumed.append(text_row["parse_outcome_id"])
        payloads, up_outcomes, hashes = {}, {}, {}
        for need in item_needs:
            rec = records[need].get(key)
            if reason is None and (rec is None or rec["fallback"]):
                # Seen but not valid now (quarantined, skipped), or never run on it.
                reason = "upstream_not_valid" if key in seen[need] else "upstream_missing"
            elif reason is None and rec["version_stale"]:
                reason = "upstream_out_of_date"
            elif reason is None:
                payloads[need] = recipe.stages[need].schema.model_validate_json(rec["payload"])
                up_outcomes[need] = UpstreamOutcome(
                    outcome=rec["outcome"],
                    reason_code=rec["reason_code"],
                    diagnostics=tuple(
                        Diagnostic.model_validate(d) for d in json.loads(rec["diagnostics"])
                    ),
                )
                hashes[need] = {
                    "payload": rec["payload_hash"],
                    "outcome": rec["outcome"],
                    "reason": rec["reason_code"],
                    "diagnostics": rec["diagnostics"],
                }
                consumed.append(rec["outcome_id"])
        source_uri = r["source_uri"]
        item = Item(
            key=key,
            item_version_id=r["item_version_id"],
            locator=r["locator"],
            role=r["role"],
            bytes_hash=r["bytes_hash"],
            size_bytes=r["size_bytes"],
            path=ws.blobs.path_for(r["bytes_hash"]),
            source=Source(name=Path(source_uri).name or source_uri, uri=source_uri),
            text=text_row["text"] if text_row and reason is None else None,
            structure=json.loads(text_row["structure"]) if text_row and reason is None else None,
            parser=text_row["parser"] if text_row and reason is None else None,
            extracted_text_id=text_row["extracted_text_id"]
            if text_row and reason is None
            else None,
            upstream=MappingProxyType(payloads),
            upstream_outcomes=MappingProxyType(up_outcomes),
        )
        fp_payload: dict[str, Any] = {
            "bytes": r["bytes_hash"],
            "text": text_row["text_hash"] if text_row else None,
            "parser": text_row["parser"] if text_row else None,
            "up": hashes,
        }
        if review_fp is not None:
            fp_payload["review"] = review_fp
        out.append((item, _fp(fp_payload), tuple(consumed), reason))
    return out


def _validate(stage: Stage, out: Any) -> RecordEnvelope:
    if isinstance(out, RecordEnvelope):
        env = out
    elif isinstance(out, BaseModel | dict):
        env = RecordEnvelope.valid(out)
    else:
        raise TypeError(
            f"stage {stage.name} returned {type(out).__name__}, not a payload or envelope"
        )
    if env.payload is None or isinstance(env.payload, stage.schema):
        return env
    raw = env.payload.model_dump() if isinstance(env.payload, BaseModel) else env.payload
    try:
        payload = stage.schema.model_validate(raw)
    except ValidationError as exc:
        return RecordEnvelope.quarantined(
            "schema_violation",
            *[
                Diagnostic(
                    code="schema_violation", field=".".join(map(str, e["loc"])), message=e["msg"]
                )
                for e in exc.errors()
            ],
        )
    return env.model_copy(update={"payload": payload})


def recipe_plan(
    ws: Workspace,
    recipe: Recipe,
    name: str,
    *,
    allow_empty: bool = False,
    params: Mapping[str, Any] | None = None,
) -> Plan:
    stage = recipe.stages[name]
    full = recipe.stage_name(name)
    stage_fp = recipe.stage_fps[name]
    effective_params = {**stage.params, **(params or {})}
    validate_stage_llm(effective_params)

    # Captured once in subjects() so every subject's input_fp and ctx.review_report()
    # see the same plan-time snapshot; a verdict mid-run shows up on the next run.
    review_json: list[str | None] = [None]

    def subjects() -> list[Subject]:
        review_fp: str | None = None
        if stage.reads_review:
            summary = client_summary(ws.conn)
            review_json[0] = canonical(summary)
            review_fp = _fp(summary)
        items = _items(ws, recipe, stage, review_fp=review_fp)
        if stage.subject == "item":
            return [
                Subject(
                    stage=full,
                    stage_fp=stage_fp,
                    kind="item",
                    key=item.key,
                    item_version_id=item.item_version_id,
                    input=item,
                    input_fp=fp,
                    consumed=consumed,
                    skip=RecordEnvelope.skipped(reason, Diagnostic(code=reason))
                    if reason
                    else None,
                )
                for item, fp, consumed, reason in items
            ]
        group_needs = [n for n in stage.needs if recipe.stages[n].subject == "group"]
        group_records: dict[str, dict[str, sqlite3.Row]] = {}
        group_seen: dict[str, set[str]] = {}
        for need in group_needs:
            full_need = recipe.stage_name(need)
            group_records[need] = {
                r["subject_key"].removeprefix("group:"): r
                for r in ws.conn.execute(
                    "SELECT * FROM current_records WHERE stage = ? AND subject_kind = 'group'"
                    " AND NOT fallback",
                    (full_need,),
                )
            }
            group_seen[need] = {
                r[0].removeprefix("group:")
                for r in ws.conn.execute(
                    "SELECT subject_key FROM current_outcomes"
                    " WHERE stage = ? AND subject_kind = 'group'",
                    (full_need,),
                )
            }
        complete = [(i, fp, c) for i, fp, c, reason in items if reason is None]
        excluded = tuple(Excluded(i.key, i.locator, reason) for i, _, _, reason in items if reason)
        if stage.subject == "run":
            has_scope = bool(
                items or any(group_records.values()) or allow_empty or stage.allow_empty
            )
            groups = {"run": (complete, excluded)} if has_scope else {}
        else:
            groups = {}
            for i, fp, c in complete:
                key = stage.group_by(i)
                if key is not None:
                    groups.setdefault(str(key), ([], []))[0].append((i, fp, c))
            # Incomplete items are placed too, so a group sees what it had to leave out and a
            # group with no complete member still exists rather than vanishing. A group_by
            # that reads upstream payloads cannot place them (they have none) and raises;
            # those stay unplaced, which the module docstring calls out.
            for i, _, _, reason in items:
                if reason is None:
                    continue
                try:
                    key = stage.group_by(i)
                except Exception:
                    continue
                if key is not None:
                    groups.setdefault(str(key), ([], []))[1].append(
                        Excluded(i.key, i.locator, reason)
                    )
            groups = {k: (m, tuple(e)) for k, (m, e) in groups.items()}
        out = []
        for key, (members, left_out) in sorted(groups.items()):
            up_payloads: dict[str, Mapping[str, BaseModel]] = {}
            up_outcomes: dict[str, Mapping[str, UpstreamOutcome]] = {}
            up_hashes: dict[str, Any] = {}
            up_consumed: list[int] = []
            skip_reason: str | None = None
            for need in group_needs:
                if stage.subject == "run":
                    selected = sorted(group_records[need].items())
                else:
                    rec = group_records[need].get(key)
                    if skip_reason is None and rec is None:
                        skip_reason = (
                            "upstream_not_valid" if key in group_seen[need] else "upstream_missing"
                        )
                    selected = [(key, rec)] if rec is not None and skip_reason is None else []
                if skip_reason is None:
                    need_schema = recipe.stages[need].schema
                    up_payloads[need] = MappingProxyType(
                        {gk: need_schema.model_validate_json(r["payload"]) for gk, r in selected}
                    )
                    up_outcomes[need] = MappingProxyType(
                        {
                            gk: UpstreamOutcome(
                                outcome=r["outcome"],
                                reason_code=r["reason_code"],
                                diagnostics=tuple(
                                    Diagnostic.model_validate(d)
                                    for d in json.loads(r["diagnostics"])
                                ),
                            )
                            for gk, r in selected
                        }
                    )
                    up_hashes[need] = {
                        gk: {
                            "payload": r["payload_hash"],
                            "outcome": r["outcome"],
                            "reason": r["reason_code"],
                            "diagnostics": r["diagnostics"],
                        }
                        for gk, r in selected
                    }
                    up_consumed.extend(r["outcome_id"] for _, r in selected)
            fp_payload: dict[str, Any] = {
                "members": sorted((m[0].key, m[1]) for m in members),
                "excluded": sorted((e.key, e.reason) for e in left_out),
            }
            if group_needs:
                fp_payload["up"] = up_hashes
            if review_fp is not None:
                fp_payload["review"] = review_fp
            out.append(
                Subject(
                    stage=full,
                    stage_fp=stage_fp,
                    kind=stage.subject,
                    key=key if stage.subject == "run" else f"group:{key}",
                    item_version_id=None,
                    input=Group(
                        key,
                        tuple(m[0] for m in members),
                        left_out,
                        upstream=MappingProxyType(up_payloads),
                        upstream_outcomes=MappingProxyType(up_outcomes),
                    ),
                    input_fp=_fp(fp_payload),
                    consumed=tuple(sorted({u for m in members for u in m[2]} | set(up_consumed))),
                    skip=RecordEnvelope.skipped(skip_reason, Diagnostic(code=skip_reason))
                    if skip_reason
                    else None,
                )
            )
        return out

    def execute(ctx: RunContext, s: Subject) -> RecordEnvelope:
        ctx._review_report_json = review_json[0]
        return _validate(stage, stage.fn(ctx, s.input))

    def store(conn: sqlite3.Connection, run_id: int, s: Subject, env: RecordEnvelope) -> dict:
        # Evidence is checked and resolved before the record is hashed, so the stored
        # payload carries its pages; an EvidenceError here fails the run.
        document, evidence_rows = resolve_evidence(env.payload, s.input)
        payload = canonical(document)
        record_id = conn.execute(
            "INSERT INTO records (stage, payload, payload_hash, created_at) VALUES (?, ?, ?, ?)",
            (full, payload, hashlib.sha256(payload.encode()).hexdigest(), utc_now()),
        ).lastrowid
        index_evidence(conn, record_id, evidence_rows)
        return {"record_id": record_id}

    return Plan(
        step=full,
        stage_fp=stage_fp,
        params=effective_params,
        subjects=subjects,
        execute=execute,
        store=store,
        recipe_fp=recipe.fingerprint,
        allow_empty=allow_empty or stage.allow_empty,
        allow_nothing_attempted=stage.allow_nothing_attempted,
        fail_if_all_quarantined=stage.fail_if_all_quarantined,
        gate=stage.gate,
    )
