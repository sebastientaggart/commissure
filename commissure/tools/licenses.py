"""Third-party license inventory and gate: what Commissure uses, under which licenses.

    commissure licenses [--json]

Government clients approve permissive licenses without review, so core and the vendored
browser assets stay permissive (AGENTS.md, "Third-party licenses"). `licenses.toml` beside
this file holds the decisions metadata cannot express: the allowlist, overrides for packages
whose metadata names no SPDX license, operator-approved exceptions, and vendored-file
provenance. Everything else is read from installed package metadata, so the inventory always
matches what is installed on this platform. Dev-only tools are not listed: they never ship.
`tests/test_licenses.py` enforces the same report this command prints.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import tomllib
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REGISTER = Path(__file__).with_name("licenses.toml")
STATIC_DIR = Path(__file__).resolve().parents[1] / "deliver" / "static"
ROOT_PACKAGE = "commissure"

# Classifiers that name exactly one SPDX license. "BSD License" is absent on purpose: it does
# not say which BSD, so such packages need an override that names it.
CLASSIFIERS = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
}

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*\Z")
_OPERATORS = {"AND", "OR", "WITH"}


# --- SPDX expressions ------------------------------------------------------------------------


def parse_spdx(text: str) -> Any | None:
    """Parse an SPDX license expression into nested tuples, or None if it is not one.

    A license id is a str; ("OR", a, b), ("AND", a, b), and ("WITH", id, exception) combine.
    """
    tokens = re.findall(r"\(|\)|[^\s()]+", text)
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def expr(op: str, operand: Any) -> Any:
        left = operand()
        while peek() == op:
            take()
            left = (op, left, operand())
        return left

    def factor() -> Any:
        tok = peek()
        if tok == "(":
            take()
            inner = expr("OR", lambda: expr("AND", factor))
            if peek() != ")":
                raise ValueError(text)
            take()
            return inner
        if tok is None or tok in _OPERATORS or not _ID.match(tok):
            raise ValueError(text)
        take()
        if peek() == "WITH":
            take()
            exc = peek()
            if exc is None or exc in _OPERATORS or not _ID.match(exc):
                raise ValueError(text)
            return ("WITH", tok, take())
        return tok

    try:
        tree = expr("OR", lambda: expr("AND", factor))
    except ValueError:
        return None
    return tree if pos == len(tokens) else None


def permitted(tree: Any, allowed: set[str]) -> bool:
    """True when the licensee may choose a branch made only of allowed licenses."""
    if isinstance(tree, str):
        return tree in allowed
    op, left, right = tree
    if op == "WITH":  # an exception only adds permissions to its base license
        return left in allowed
    if op == "OR":
        return permitted(left, allowed) or permitted(right, allowed)
    return permitted(left, allowed) and permitted(right, allowed)


# --- Inventory -------------------------------------------------------------------------------


@dataclass
class Component:
    name: str
    scope: str  # "core", "extra:<name>", or "vendored"
    version: str | None = None  # None when not installed
    declared: str | None = None  # the license as the metadata states it
    license: str | None = None  # resolved SPDX expression
    status: str = ""  # allowed, exception, not allowed, unresolved, not installed
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    allowed: list[str]
    components: list[Component]
    exceptions: list[dict[str, Any]]
    problems: list[str]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def load_register(path: Path = REGISTER) -> dict[str, Any]:
    register = tomllib.loads(path.read_text(encoding="utf-8"))
    register["packages"] = {
        canonicalize_name(k): v for k, v in register.get("packages", {}).items()
    }
    return register


def _closure(
    root: str, extras: frozenset[str]
) -> dict[str, importlib.metadata.Distribution | None]:
    """Every package `root[extras]` requires on this platform, by canonical name.

    A package that is required but not installed maps to None and is not walked further.
    """
    found: dict[str, importlib.metadata.Distribution | None] = {}
    seen: set[tuple[str, frozenset[str]]] = set()
    queue: deque[tuple[str, frozenset[str]]] = deque([(root, extras)])
    while queue:
        name, active = queue.popleft()
        key = canonicalize_name(name)
        if (key, active) in seen:
            continue
        seen.add((key, active))
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            found.setdefault(key, None)
            continue
        found[key] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None:
                envs = [{"extra": ""}] + [{"extra": ex} for ex in active]
                if not any(req.marker.evaluate(env) for env in envs):
                    continue
            queue.append((req.name, frozenset(canonicalize_name(e) for e in req.extras)))
    return found


def declared_license(dist: importlib.metadata.Distribution) -> tuple[str | None, str | None]:
    """Return (what the metadata declares, the SPDX expression it unambiguously names)."""
    md = dist.metadata
    expression = md.get("License-Expression")
    if expression:
        return expression, expression if parse_spdx(expression) else None
    text = (md.get("License") or "").strip().splitlines()
    text = text[0].strip() if text else ""
    classifiers = [
        c.rsplit(" :: ", 1)[-1]
        for c in md.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    declared = text or "; ".join(classifiers) or None
    if text and parse_spdx(text):
        return declared, text
    mapped = {CLASSIFIERS.get(c) for c in classifiers}
    if len(mapped) == 1 and None not in mapped:
        return declared, mapped.pop()
    return declared, None


def _python_components(register: dict[str, Any], problems: list[str]) -> list[Component]:
    overrides = register["packages"]
    core = _closure(ROOT_PACKAGE, frozenset())
    scopes: dict[str, tuple[str, importlib.metadata.Distribution | None]] = {
        key: ("core", dist) for key, dist in core.items() if key != ROOT_PACKAGE
    }
    root = importlib.metadata.distribution(ROOT_PACKAGE)
    for extra in sorted(root.metadata.get_all("Provides-Extra") or []):
        for key, dist in _closure(ROOT_PACKAGE, frozenset({extra})).items():
            if key != ROOT_PACKAGE:
                scopes.setdefault(key, (f"extra:{extra}", dist))

    components = []
    for key, (scope, dist) in sorted(scopes.items()):
        if dist is None:
            components.append(Component(name=key, scope=scope, status="not installed"))
            continue
        declared, spdx = declared_license(dist)
        comp = Component(
            name=dist.metadata["Name"], scope=scope, version=dist.version, declared=declared
        )
        override = overrides.get(key)
        if override is not None:
            if override["declared"] == declared:
                spdx = override["license"]
                comp.notes.append(override["note"])
            else:
                problems.append(
                    f"{comp.name}: metadata now declares {declared!r}, not the "
                    f"{override['declared']!r} its override in licenses.toml was written for; "
                    "re-review the license and update or remove the override"
                )
        comp.license = spdx
        components.append(comp)

    for key in overrides:
        if key not in scopes:
            problems.append(f"licenses.toml overrides {key}, which Commissure does not use")
    return components


def _vendored_components(
    register: dict[str, Any], allowed: set[str], static_dir: Path, problems: list[str]
) -> list[Component]:
    entries = register.get("vendored", [])
    listed = {e["file"] for e in entries} | set(register.get("first_party", []))
    license_files = {e["license_file"] for e in entries}
    for path in sorted(static_dir.rglob("*")):
        rel = path.relative_to(static_dir).as_posix()
        if not path.is_file() or path.name.startswith("."):
            continue
        if rel.startswith("licenses/"):
            if rel not in license_files:
                problems.append(f"static/{rel} is the license of no vendored entry")
        elif rel not in listed:
            problems.append(
                f"static/{rel} is not in licenses.toml: add a [[vendored]] entry (with its "
                "license file) or list it in first_party"
            )
    for name in register.get("first_party", []):
        if not (static_dir / name).is_file():
            problems.append(f"licenses.toml lists first-party static/{name}, which is missing")

    components = []
    for e in entries:
        comp = Component(
            name=e["package"],
            scope="vendored",
            version=e["version"],
            declared=e["license"],
            license=e["license"],
            notes=[f"static/{e['file']}"] + ([e["note"]] if "note" in e else []),
        )
        path = static_dir / e["file"]
        if not path.is_file():
            problems.append(f"vendored static/{e['file']} is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != e["sha256"]:
            problems.append(
                f"static/{e['file']} does not match its sha256 in licenses.toml: if it was "
                "upgraded on purpose, update version, source, source_sha256, sha256, and the "
                "license file together"
            )
        if not (static_dir / e["license_file"]).is_file():
            problems.append(f"{e['package']}: license file static/{e['license_file']} is missing")
        tree = parse_spdx(e["license"])
        comp.status = "allowed" if tree is not None and permitted(tree, allowed) else "not allowed"
        if comp.status == "not allowed":
            problems.append(f"vendored {e['package']} is {e['license']}, not in the allowlist")
        components.append(comp)
    return components


def build_report(register: dict[str, Any] | None = None, static_dir: Path = STATIC_DIR) -> Report:
    """Resolve every component's license against the register and list what is wrong."""
    register = load_register() if register is None else register
    allowed = set(register["allowed"])
    exceptions = register.get("exception", [])
    problems: list[str] = []

    components = _python_components(register, problems)
    by_name = {canonicalize_name(c.name): c for c in components}
    for exc in exceptions:
        comp = by_name.get(canonicalize_name(exc["package"]))
        if comp is None:
            problems.append(f"licenses.toml has an exception for {exc['package']}, which is unused")
        elif "component" in exc:
            comp.notes.append(f"bundles {exc['component']} ({exc['license']}): {exc['reason']}")
        elif comp.status == "not installed":
            comp.notes.append(f"{exc['license']} by exception ({exc['scope']}): {exc['reason']}")

    for comp in components:
        if comp.status == "not installed":
            continue
        tree = parse_spdx(comp.license) if comp.license else None
        if tree is None:
            comp.status = "unresolved"
            problems.append(
                f"{comp.name} ({comp.scope}): metadata declares {comp.declared!r}, which names no "
                "SPDX license; read its license file and add an override to licenses.toml"
            )
            continue
        if permitted(tree, allowed):
            comp.status = "allowed"
            continue
        match = [
            e
            for e in exceptions
            if canonicalize_name(e["package"]) == canonicalize_name(comp.name)
            and "component" not in e
        ]
        if any(e["license"] == comp.license and e["scope"] == comp.scope for e in match):
            comp.status = "exception"
            comp.notes.extend(e["reason"] for e in match)
            continue
        comp.status = "not allowed"
        why = (
            f"its exception covers {match[0]['license']} in {match[0]['scope']}"
            if match
            else "no exception in licenses.toml covers it"
        )
        problems.append(f"{comp.name} ({comp.scope}) is {comp.license}, not allowed: {why}")

    # An exception scoped elsewhere than its package is required. When the package is installed
    # the license check above already reported it; this catches it when it is not installed.
    for exc in exceptions:
        comp = by_name.get(canonicalize_name(exc["package"]))
        if (
            comp is not None
            and "component" not in exc
            and comp.scope != exc["scope"]
            and comp.status != "not allowed"
        ):
            problems.append(
                f"licenses.toml scopes {exc['package']} to {exc['scope']}, but it is "
                f"required by {comp.scope}"
            )

    components.extend(_vendored_components(register, allowed, static_dir, problems))
    return Report(sorted(allowed), components, exceptions, problems)
