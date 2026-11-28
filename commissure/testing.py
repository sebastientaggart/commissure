"""Public test harness and pytest plugin for Commissure solutions.

Solution tests drive a recipe through the same CLI a person runs and inspect its outcomes
without hand-assembling `CliRunner` invocations or querying internal SQLite tables:

    def test_stage(commissure_run):
        assert commissure_run.ingest("intake/comments", role="comment").exit_code == 0
        for report in commissure_run.steps("parse", "extract", "tally").values():
            assert report.exit_code == 0, report
        records = commissure_run.records("extract")
        assert records["c09.html"].outcome == "warning"
        assert records["c09.html"].reason == "no_position_phrase"

Registration uses `[project.entry-points.pytest11]` (`commissure = "commissure.testing"`)
rather than `pytest_plugins` in each solution's `conftest.py`:

- Only `pytest` reads `pytest11` entry points, so `import commissure`, `import commissure.api`,
  and every CLI command never import `commissure.testing` or `pytest`.
- Every scaffolded or upgraded solution gets the `commissure_run` fixture automatically without
  needing a `tests/conftest.py` file in the project template.
- In any environment where `pytest` runs, the plugin defines only a fixture (no hooks or
  collection side effects), so it is inert unless a test requests `commissure_run`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from commissure.cli import app
from commissure.inspect import _stage_matches, open_readonly
from commissure.recipes import Recipe, load_recipe
from commissure.workspace import DB_NAME

_REVIEW_WITH_RECIPE = frozenset({"checks", "sample", "next", "spot"})
_REVIEW_WITH_REVIEWER = frozenset({"sample", "record"})


class RunReport(dict[str, Any]):
    """Parsed JSON report of one CLI command, alongside its exit code and raw streams.

    Never asserts on `exit_code`: a failing stage's exit code reaches the test so failure
    paths and gates can be asserted directly.
    """

    def __init__(
        self,
        exit_code: int,
        payload: dict[str, Any],
        *,
        stdout: str = "",
        stderr: str = "",
        output: str = "",
    ) -> None:
        data = dict(payload)
        data.setdefault("exit_code", exit_code)
        if exit_code != 0 and "error" not in data and stderr.strip():
            data["error"] = stderr.strip()
        super().__init__(data)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.output = output

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class OutcomeRecord(dict[str, Any]):
    """One subject's current outcome for a stage: `{outcome, reason, codes, payload}`."""

    def __init__(
        self,
        *,
        outcome: str,
        reason: str | None,
        codes: list[str],
        payload: Any | None,
    ) -> None:
        super().__init__(outcome=outcome, reason=reason, codes=codes, payload=payload)

    @property
    def outcome(self) -> str:
        return self["outcome"]

    @property
    def reason(self) -> str | None:
        return self["reason"]

    @property
    def codes(self) -> list[str]:
        return self["codes"]

    @property
    def payload(self) -> Any | None:
        return self["payload"]


class Cli:
    """In-process `CliRunner` wrapper around `commissure.cli.app`."""

    def __init__(self) -> None:
        self.runner = CliRunner()

    def __call__(self, *args: str, env: dict[str, str] | None = None):
        return self.runner.invoke(app, list(args), env=env)

    def ingest(self, ws_root: Path, source: Path, *extra: str) -> tuple[int, dict[str, Any]]:
        result = self(
            "run",
            "--step",
            "ingest",
            "--workspace",
            str(ws_root),
            "--source",
            str(source),
            "--json",
            *extra,
        )
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        return result.exit_code, payload


class Pipeline:
    """Test driver bound to one workspace under `tmp_path` and one solution `recipe/` folder."""

    def __init__(
        self,
        workspace: Path,
        recipe: Path | None = None,
        *,
        reviewer: str = "tester",
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.recipe = Path(recipe).resolve() if recipe is not None else None
        self.reviewer = reviewer
        self.cli = Cli()
        self.reports: dict[str, RunReport] = {}
        self._staged_sources: dict[Path, Path] = {}

    def load(self) -> Recipe:
        """Load and validate the bound `recipe/` folder through `load_recipe`."""
        if self.recipe is None:
            raise ValueError("Pipeline has no bound recipe folder")
        return load_recipe(self.recipe)

    def _invoke_json(self, *args: str) -> RunReport:
        result = self.cli(*args)
        payload: dict[str, Any] = {}
        raw = result.stdout.strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                pass
        return RunReport(
            result.exit_code,
            payload,
            stdout=result.stdout,
            stderr=result.stderr,
            output=result.output,
        )

    def _stage_source(self, source: Path | str) -> Path:
        """Copy an input folder outside the workspace's scratch parent into it before ingest.

        Originals in a solution's `intake/` are read-only (`AGENTS.md`, Section 7). A folder
        already inside `self.workspace.parent` (a test-created folder in `tmp_path`) is used
        in place so drift tests can edit files there and re-ingest.
        """
        raw = Path(source)
        if not raw.is_absolute() and self.recipe is not None:
            candidate = self.recipe.parent / raw
            resolved = candidate.resolve() if candidate.exists() else raw.resolve()
        else:
            resolved = raw.resolve()

        scratch_root = self.workspace.parent
        if not resolved.is_dir() or resolved.is_relative_to(scratch_root):
            return resolved
        if resolved in self._staged_sources:
            return self._staged_sources[resolved]

        intake_root = scratch_root / "intake"
        intake_root.mkdir(parents=True, exist_ok=True)
        dest = intake_root / resolved.name
        if dest.exists():
            dest = intake_root / f"{len(self._staged_sources) + 1}_{resolved.name}"
        shutil.copytree(resolved, dest)
        self._staged_sources[resolved] = dest
        return dest

    def ingest(self, source: Path | str, role: str = "corpus", *flags: str) -> RunReport:
        """Run `commissure run --step ingest` on `source` (staging external folders first)."""
        staged = self._stage_source(source)
        args = [
            "run",
            "--step",
            "ingest",
            "--workspace",
            str(self.workspace),
            "--source",
            str(staged),
            "--role",
            role,
        ]
        if (
            self.recipe is not None
            and (self.recipe / "recipe.py").is_file()
            and "--recipe" not in flags
        ):
            args.extend(["--recipe", str(self.recipe)])
        args.extend(flags)
        if "--json" not in flags:
            args.append("--json")
        report = self._invoke_json(*args)
        self.reports["ingest"] = report
        return report

    def parse(self, parser: str | None = None, *flags: str) -> RunReport:
        """Run `commissure run --step parse` (optionally with `--parser <parser>`)."""
        args = ["run", "--step", "parse", "--workspace", str(self.workspace)]
        if parser is not None:
            args.extend(["--parser", parser])
        if (
            self.recipe is not None
            and (self.recipe / "recipe.py").is_file()
            and "--recipe" not in flags
        ):
            args.extend(["--recipe", str(self.recipe)])
        args.extend(flags)
        if "--json" not in flags:
            args.append("--json")
        report = self._invoke_json(*args)
        key = f"parse.{parser}" if parser else "parse"
        self.reports[key] = report
        if parser is None:
            self.reports["parse"] = report
        return report

    def stage(self, name: str, *flags: str) -> RunReport:
        """Run `commissure run --step <name>` against the bound workspace and recipe."""
        if name == "parse":
            return self.parse(None, *flags)
        args = ["run", "--step", name, "--workspace", str(self.workspace)]
        if self.recipe is not None and "--recipe" not in flags:
            args.extend(["--recipe", str(self.recipe)])
        args.extend(flags)
        if "--json" not in flags:
            args.append("--json")
        report = self._invoke_json(*args)
        self.reports[name] = report
        return report

    def steps(self, *names: str) -> dict[str, RunReport]:
        """Run each named step (`"parse"` or a recipe stage name) in order and return reports."""
        return {name: self.stage(name) for name in names}

    def records(self, stage: str) -> dict[str, OutcomeRecord]:
        """Current outcomes for `stage`, keyed by locator (`item`), group key, or `"run"`.

        Each value is an `OutcomeRecord(outcome=..., reason=..., codes=[...], payload=...)`.
        Items set aside at parse (`quarantined` or `skipped` upstream) appear as `skipped`
        outcomes on downstream item stages rather than vanishing.
        """
        db_path = self.workspace / DB_NAME
        if not db_path.is_file():
            return {}
        conn = open_readonly(db_path)
        try:
            rows = conn.execute(
                """
                SELECT co.outcome_id, co.stage, co.subject_kind, co.subject_key,
                       co.outcome, co.reason_code, co.diagnostics, r.payload, i.locator
                FROM current_outcomes co
                LEFT JOIN records r ON r.id = co.record_id
                LEFT JOIN item_versions v ON v.id = co.item_version_id
                LEFT JOIN items i ON i.id = coalesce(
                    v.item_id,
                    CASE WHEN co.subject_kind = 'item' AND substr(co.subject_key, 1, 5) = 'item:'
                              AND substr(co.subject_key, 6) GLOB '[0-9]*'
                         THEN CAST(substr(co.subject_key, 6) AS INTEGER) END
                )
                ORDER BY co.outcome_id
                """
            ).fetchall()
        finally:
            conn.close()

        out: dict[str, OutcomeRecord] = {}
        for row in rows:
            if not _stage_matches(row["stage"], stage):
                continue
            kind = row["subject_kind"]
            if kind == "item":
                key = row["locator"] or row["subject_key"]
            elif kind == "group":
                key = row["subject_key"].removeprefix("group:")
            else:
                key = row["subject_key"]
            diags = json.loads(row["diagnostics"]) if row["diagnostics"] else []
            codes = [d["code"] for d in diags if isinstance(d, dict) and "code" in d]
            payload = json.loads(row["payload"]) if row["payload"] is not None else None
            out[key] = OutcomeRecord(
                outcome=row["outcome"],
                reason=row["reason_code"],
                codes=codes,
                payload=payload,
            )
        return out

    def review(
        self,
        subcommand: str,
        *args: str,
        reviewer: str | None = None,
    ) -> RunReport:
        """Run `commissure review <subcommand>`, attaching `--recipe` and `--reviewer` as needed."""
        cmd = ["review", subcommand, *args]
        if "--workspace" not in args:
            cmd.extend(["--workspace", str(self.workspace)])
        if (
            subcommand in _REVIEW_WITH_RECIPE
            and self.recipe is not None
            and (self.recipe / "recipe.py").is_file()
            and "--recipe" not in args
        ):
            cmd.extend(["--recipe", str(self.recipe)])
        if subcommand in _REVIEW_WITH_REVIEWER and "--reviewer" not in args:
            cmd.extend(["--reviewer", reviewer or self.reviewer])
        if "--json" not in args:
            cmd.append("--json")
        return self._invoke_json(*cmd)


def _discover_recipe(test_path: Path) -> Path | None:
    start = test_path if test_path.is_dir() else test_path.parent
    for folder in (start, *start.parents):
        candidate = folder / "recipe"
        if (candidate / "recipe.py").is_file():
            return candidate
    cwd_candidate = Path.cwd() / "recipe"
    if (cwd_candidate / "recipe.py").is_file():
        return cwd_candidate
    return None


try:
    import pytest
except ImportError:  # pragma: no cover - pytest is a dev dependency
    pass
else:

    @pytest.fixture
    def commissure_run(tmp_path: Path, request: pytest.FixtureRequest) -> Pipeline:
        """A fresh `Pipeline` bound to `tmp_path / "workspace"` and the solution's `recipe/`."""
        return Pipeline(tmp_path / "workspace", _discover_recipe(Path(request.path)))
