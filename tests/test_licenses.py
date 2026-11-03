"""Third-party license gate (commissure/tools/licenses.py) and the docxtpl import boundary.

Core runtime dependencies and vendored browser assets stay permissive so core ships without
license review; copyleft libraries (docxtpl, LGPL) live only in optional extras that core never
imports. Each check below is shown failing against a doctored register or static folder.
"""

from __future__ import annotations

import ast
import copy
import json
import shutil
from pathlib import Path

import pytest

from commissure.tools import licenses
from commissure.tools.licenses import build_report, load_register, parse_spdx, permitted

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def register() -> dict:
    return copy.deepcopy(load_register())


@pytest.fixture
def static(tmp_path: Path) -> Path:
    dest = tmp_path / "static"
    shutil.copytree(licenses.STATIC_DIR, dest)
    return dest


def _forbidden_imports(root: Path, forbidden: set[str]) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top in forbidden:
                        hits.append(f"{rel}:{node.lineno}:import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".", 1)[0]
                if top in forbidden:
                    hits.append(f"{rel}:{node.lineno}:from {node.module}")
    return hits


def test_installed_components_satisfy_the_register() -> None:
    report = build_report()
    assert report.problems == []
    by_name = {c.name.lower(): c for c in report.components}
    assert by_name["jinja2"].scope == "core"
    assert by_name["docxtpl"].scope == "extra:docx"
    assert "commissure" not in by_name
    assert {c.name for c in report.components if c.scope == "vendored"} == {
        "htmx.org",
        "chart.js",
        "cytoscape",
        "@picocss/pico",
    }


def test_optional_extras_are_installed_for_the_gate() -> None:
    missing = [
        f"{c.name} ({c.scope})" for c in build_report().components if c.status == "not installed"
    ]
    if missing:
        pytest.skip(f"extras not installed, licenses unchecked: {', '.join(missing)}")


def test_core_never_imports_docxtpl(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from docxtpl import DocxTemplate\n", encoding="utf-8")
    assert _forbidden_imports(tmp_path, {"docxtpl"}) == ["bad.py:1:from docxtpl"]

    assert _forbidden_imports(REPO_ROOT / "commissure", {"docxtpl"}) == []


def test_parse_spdx_and_permitted() -> None:
    allowed = {"MIT", "Apache-2.0"}
    assert parse_spdx("MIT") == "MIT"
    assert parse_spdx("Apache-2.0 OR BSD-3-Clause") == ("OR", "Apache-2.0", "BSD-3-Clause")
    assert parse_spdx("(MIT OR GPL-3.0-only) AND Apache-2.0") == (
        "AND",
        ("OR", "MIT", "GPL-3.0-only"),
        "Apache-2.0",
    )
    for bad in ("Apache License 2.0", "BSD-3-Clause, Apache-2.0", "MIT OR", "(MIT", ""):
        assert parse_spdx(bad) is None, bad
    assert permitted(parse_spdx("GPL-3.0-only OR MIT"), allowed)
    assert not permitted(parse_spdx("MIT AND GPL-3.0-only"), allowed)
    assert permitted(parse_spdx("Apache-2.0 WITH LLVM-exception"), allowed)


def test_license_outside_allowlist_without_exception_fails(register: dict) -> None:
    register["allowed"].remove("PSF-2.0")
    problems = build_report(register).problems
    assert any(p.startswith("typing_extensions (core) is PSF-2.0, not allowed") for p in problems)


def test_exception_in_another_scope_does_not_cover_core(register: dict) -> None:
    register["allowed"].remove("PSF-2.0")
    register["exception"].append(
        {"package": "typing_extensions", "license": "PSF-2.0", "scope": "extra:docx",
         "reason": "test"}
    )  # fmt: skip
    problems = build_report(register).problems
    assert any("its exception covers PSF-2.0 in extra:docx" in p for p in problems)

    register["exception"][-1]["scope"] = "core"
    assert build_report(register).problems == []


def test_extra_exception_moved_to_core_fails(register: dict) -> None:
    docxtpl = next(e for e in register["exception"] if e["package"] == "docxtpl")
    docxtpl["scope"] = "core"
    problems = build_report(register).problems
    assert any("docxtpl" in p for p in problems), problems


def test_override_stops_applying_when_metadata_changes(register: dict) -> None:
    register["packages"]["jinja2"]["declared"] = "MIT License"
    problems = build_report(register).problems
    assert any("Jinja2: metadata now declares 'BSD License'" in p for p in problems)


def test_ambiguous_metadata_without_override_is_unresolved(register: dict) -> None:
    del register["packages"]["jinja2"]
    problems = build_report(register).problems
    assert any(p.startswith("Jinja2 (core): metadata declares 'BSD License'") for p in problems)


def test_stale_register_entries_fail(register: dict) -> None:
    register["packages"]["left-pad"] = {"declared": "x", "license": "MIT", "note": "x"}
    register["exception"].append(
        {"package": "left-pad", "license": "MIT", "scope": "core", "reason": "x"}
    )
    problems = build_report(register).problems
    assert "licenses.toml overrides left-pad, which Commissure does not use" in problems
    assert "licenses.toml has an exception for left-pad, which is unused" in problems


def test_unlisted_static_file_fails(register: dict, static: Path) -> None:
    (static / "new.min.js").write_text("x", encoding="utf-8")
    (static / "licenses" / "new.LICENSE.txt").write_text("x", encoding="utf-8")
    problems = build_report(register, static).problems
    assert any(p.startswith("static/new.min.js is not in licenses.toml") for p in problems)
    assert "static/licenses/new.LICENSE.txt is the license of no vendored entry" in problems


def test_changed_vendored_file_fails(register: dict, static: Path) -> None:
    with (static / "htmx.min.js").open("a", encoding="utf-8") as f:
        f.write("\n")
    (static / "licenses" / "pico.LICENSE.md").unlink()
    problems = build_report(register, static).problems
    assert any(p.startswith("static/htmx.min.js does not match its sha256") for p in problems)
    assert any("license file static/licenses/pico.LICENSE.md is missing" in p for p in problems)


def test_copyleft_vendored_asset_fails(register: dict) -> None:
    register["vendored"][0]["license"] = "GPL-3.0-only"
    problems = build_report(register).problems
    assert "vendored htmx.org is GPL-3.0-only, not in the allowlist" in problems


def test_cli_reports_inventory_and_exit_code(cli, monkeypatch, register: dict) -> None:
    result = cli("licenses", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["problems"] == []
    assert any(c["name"] == "lxml" and c["notes"] for c in report["components"])

    register["allowed"].remove("MIT")
    monkeypatch.setattr(licenses, "load_register", lambda: register)
    assert cli("licenses").exit_code == 1
