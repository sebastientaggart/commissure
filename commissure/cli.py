"""Command line interface.

Commands (in `solutions/<name>/`, `--recipe` and `--workspace` default to `recipe/`, `workspace/`):

    commissure init solutions/<name> [--json]
    commissure probe <path> [--json]
    commissure run --step ingest --source <dir> --role <role> [--workspace W] [--json]
    commissure run --step parse [--parser default|ocr|...] [--workspace W] [--limit N] [--json]
    commissure run --step <stage> [--recipe R] [--workspace W] [--limit N] [--fresh]
                                  [--no-cache] [--param K=V] [--json]
    commissure recon [--workspace W] [--json]
    commissure inspect [--workspace W] [--run <id|latest|failed>] [--stage S] [--subject K]
                       [--call ID] [--only-errors] [--limit N] [--json]
    commissure search <query> [--workspace W] [--limit N] [--json]
    commissure serve [--recipe R] [--workspace W] [--host 127.0.0.1] [--port 8000]
    commissure release build <target> [--skip-tests] [--offline] [--allow-dirty] [--json]
    commissure release publish <target> [--tarball] [--push] [--json]

Exit codes are deterministic so OS schedulers can act on them:

    0  run succeeded, or was a clean noop; probe inspected at least one file;
       inspect, search, or recon read the workspace
    1  run failed (recorded on the run row); probe found nothing, or every
       inspectable file errored; search hit a non-usage SQLite error
    2  usage or configuration error; nothing was run
    3  workspace is locked by another process; nothing was run
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from commissure import review
from commissure.ingest import check_folder_source, effective_ignore, ingest_folder
from commissure.inspect import _resolve_subject_keys, inspect_workspaces, open_readonly
from commissure.inspect import render as render_inspect
from commissure.limits import LimitExceeded, resolve_limits
from commissure.llm import validate_stage_llm
from commissure.parse import PARSERS, ParserUnavailable
from commissure.parse.step import parse_plan
from commissure.probe import probe_failed, probe_path
from commissure.probe.render import render
from commissure.progress import ProgressReporter
from commissure.recipes import (
    RecipeError,
    load_recipe,
    load_recipe_limits,
    recipe_plan,
    register_stages,
)
from commissure.recon import DEFAULT_TEXT as RECON_DEFAULT_TEXT
from commissure.recon import recon_workspace
from commissure.recon import render as render_recon
from commissure.runner import reap_abandoned, run_plan
from commissure.scaffold import ScaffoldError, scaffold_solution
from commissure.search import DEFAULT_LIMIT, SearchUsageError, search
from commissure.serve import InterfaceApp, make_server
from commissure.tools import licenses, release
from commissure.workspace import (
    DB_NAME,
    MIGRATIONS,
    WorkspaceError,
    WorkspaceLocked,
    lock,
    open_workspace,
)

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED = 0, 1, 2, 3

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _fail_usage(message: str) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(EXIT_USAGE)


def _resolved_config(ctx: typer.Context) -> dict[str, Any]:
    """Every parameter's value and which configuration source supplied it."""
    config = {}
    for name, value in ctx.params.items():
        source = ctx.get_parameter_source(name)
        if isinstance(value, Path):
            serialized: Any = str(value)
        elif isinstance(value, tuple):
            serialized = list(value)
        else:
            serialized = value
        config[name] = {
            "value": serialized,
            "from": source.name.lower() if source else "unknown",
        }
    return config


def _parse_params(entries: list[str] | None) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for raw in entries or ():
        if "=" not in raw:
            _fail_usage(f"invalid --param {raw!r}; expected KEY=VALUE")
        key, raw_val = raw.split("=", 1)
        key = key.strip()
        if not key:
            _fail_usage(f"invalid --param {raw!r}; key must not be empty")
        try:
            val = json.loads(
                raw_val,
                parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)),
            )
        except ValueError:
            val = raw_val
        overrides[key] = val
    return overrides


@app.command()
def probe(
    path: Annotated[Path, typer.Argument(help="File or folder to inspect.")],
    sample_rows: Annotated[int, typer.Option(min=0, help="Sample rows per CSV/XLSX sheet.")] = 3,
    recipe: Annotated[
        Path | None,
        typer.Option(
            envvar="COMMISSURE_RECIPE",
            help="Optional recipe folder supplying LIMITS overrides.",
        ),
    ] = None,
    ignore: Annotated[
        list[str] | None,
        typer.Option("--ignore", help="Additional filename or locator glob to ignore."),
    ] = None,
    no_default_ignore: Annotated[
        bool,
        typer.Option("--no-default-ignore", help="Do not apply the default folder ignore list."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Inspect files directly, without a workspace. Writes nothing."""
    try:
        recipe_limits = None
        if recipe is not None:
            recipe_limits = load_recipe_limits(recipe)
        else:
            cwd = Path.cwd().resolve()
            if (cwd / "recipe" / "recipe.py").is_file() and path.resolve().is_relative_to(cwd):
                recipe_limits = load_recipe_limits(cwd / "recipe")
        limits, _ = resolve_limits(recipe_limits)
        report = probe_path(
            path,
            sample_rows=sample_rows,
            ignore=effective_ignore(ignore, no_default_ignore=no_default_ignore),
            limits=limits,
        )
    except (RecipeError, ValueError, LimitExceeded) as exc:
        _fail_usage(str(exc))
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        render(report, Console())
    raise typer.Exit(EXIT_FAILED if probe_failed(report) else EXIT_OK)


@app.command()
def inspect(
    workspace: Annotated[
        list[Path] | None,
        typer.Option(
            "--workspace",
            help="Workspace directory. Repeat to compare several. Reads state.db only.",
        ),
    ] = None,
    run_selector: Annotated[
        str | None,
        typer.Option(
            "--run",
            help="Filter to one run ID, 'latest', or 'failed'.",
        ),
    ] = None,
    stage: Annotated[
        str | None,
        typer.Option(
            "--stage",
            help="Filter to a stage name (short like 'stance' or qualified like 'recipe.stance').",
        ),
    ] = None,
    subject: Annotated[
        str | None,
        typer.Option(
            "--subject",
            help="Drill into one subject: an item or group key ('item:1', 'group:X'), a locator,"
            " or a file name only one item has. Shows originals, page links, text, and records;"
            " exit 2 when nothing or several items match.",
        ),
    ] = None,
    call: Annotated[
        int | None,
        typer.Option(
            "--call",
            min=1,
            help="Inspect one llm_calls row ID with its cached request and raw_response.",
        ),
    ] = None,
    only_errors: Annotated[
        bool,
        typer.Option(
            "--only-errors",
            help="Show only failed runs, quarantined outcomes, and non-ok LLM calls.",
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help="Cap row-level lists (runs, issues, llm_calls) to the newest N rows.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show runs, item counts, and quarantine diagnostics. Writes nothing."""
    roots = _inspect_roots(workspace)
    try:
        report = inspect_workspaces(
            roots,
            run=run_selector,
            stage=stage,
            subject=subject,
            call=call,
            only_errors=only_errors,
            limit=limit,
        )
    except (WorkspaceError, ValueError) as exc:
        _fail_usage(str(exc))
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        render_inspect(report, Console())
    raise typer.Exit(EXIT_OK)


@app.command("search")
def search_cmd(
    query: Annotated[str, typer.Argument(help="Phrase to find, or a raw FTS5 MATCH with --raw.")],
    workspace: Annotated[
        Path | None,
        typer.Option(
            envvar="COMMISSURE_WORKSPACE",
            help="Workspace directory. Reads state.db only.",
        ),
    ] = None,
    parser: Annotated[
        str | None, typer.Option(help="Search only this parser row (must be a registered parser).")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, help="Maximum hits to return.")] = DEFAULT_LIMIT,
    phrase: Annotated[
        bool, typer.Option("--phrase", help="Treat QUERY as one literal phrase (default).")
    ] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Pass QUERY to FTS5 MATCH unchanged.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Search current parsed text. Writes nothing; does not lock or migrate."""
    if phrase and raw:
        _fail_usage("pass at most one of --phrase and --raw")
    use_phrase = not raw
    root = _search_root(workspace)
    db_path = root / DB_NAME
    if not root.is_dir():
        _fail_usage(f"workspace path {root} is not a directory")
    if not db_path.is_file():
        _fail_usage(f"{root} has no {DB_NAME}")
    conn: sqlite3.Connection | None = None
    try:
        try:
            conn = open_readonly(db_path)
        except WorkspaceError as exc:
            _fail_usage(str(exc))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != len(MIGRATIONS):
            _fail_usage(
                f"{root}: schema version {version}, this code expects {len(MIGRATIONS)}; "
                "search does not migrate"
            )
        hits = search(conn, query, parser=parser, limit=limit, phrase=use_phrase)
    except SearchUsageError as exc:
        _fail_usage(str(exc))
    except sqlite3.Error as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    finally:
        if conn is not None:
            conn.close()
    report = {
        "query": query,
        "mode": "phrase" if use_phrase else "raw",
        "parser": parser,
        "limit": limit,
        "hits": [h.as_dict() for h in hits],
    }
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        table = Table("locator", "parser", "page", "snippet", title_justify="left")
        for hit in hits:
            table.add_row(
                escape(hit.locator),
                escape(hit.parser),
                "" if hit.page is None else str(hit.page),
                escape(hit.snippet),
            )
        Console().print(table)
    raise typer.Exit(EXIT_OK)


@app.command()
def recon(
    workspace: Annotated[
        Path | None,
        typer.Option(
            envvar="COMMISSURE_WORKSPACE",
            help="Workspace directory. Reads state.db only.",
        ),
    ] = None,
    text: Annotated[
        list[str] | None,
        typer.Option(
            "--text",
            help="Parser preference, in order (repeatable). Default: ocr then default.",
        ),
    ] = None,
    min_repeat: Annotated[
        int,
        typer.Option(
            "--min-repeat",
            min=1,
            help="Minimum pages, occurrences, or files for a line.",
        ),
    ] = 2,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Measure repeated lines, identical texts, table first rows, and unreadable pages.

    Writes nothing; does not lock or migrate. Prefer a scratch workspace already
    ingested and parsed (for example /tmp/commissure-recon-<name>).
    """
    root = _search_root(workspace)
    preference = tuple(text) if text else RECON_DEFAULT_TEXT
    try:
        report = recon_workspace(root, preference=preference, min_repeat=min_repeat)
    except WorkspaceError as exc:
        _fail_usage(str(exc))
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        render_recon(report, Console())
    raise typer.Exit(EXIT_OK)


def _inspect_roots(workspaces: list[Path] | None) -> list[Path]:
    """Workspaces named on the command line, else the env var, else a solution directory.

    A missing path is left for `inspect_workspace` to reject. This function never
    creates a directory.
    """
    if workspaces:
        return workspaces
    env = os.environ.get("COMMISSURE_WORKSPACE")
    if env:
        return [Path(env)]
    cwd = Path.cwd()
    if (cwd / "recipe").is_dir():
        return [cwd / "workspace"]
    _fail_usage(
        "--workspace is required (or set COMMISSURE_WORKSPACE, or run from a solution"
        " directory containing recipe/)"
    )


def _search_root(workspace: Path | None) -> Path:
    """One workspace: the flag, else the env var, else a solution directory."""
    if workspace is not None:
        return workspace
    env = os.environ.get("COMMISSURE_WORKSPACE")
    if env:
        return Path(env)
    cwd = Path.cwd()
    if (cwd / "recipe").is_dir():
        return cwd / "workspace"
    _fail_usage(
        "--workspace is required (or set COMMISSURE_WORKSPACE, or run from a solution"
        " directory containing recipe/)"
    )


CORE_STEPS = ("ingest", "parse")
# Ingest takes no --limit: a partial scan would classify every unseen locator as removed.
ALLOWED = {
    "ingest": {"recipe", "source", "role", "ignore", "no_default_ignore"},
    "parse": {"recipe", "parser", "limit", "workers"},
    "recipe": {"recipe", "limit", "workers", "fresh", "param", "no_cache"},
}
DEFAULT_WORKERS = min(4, os.cpu_count() or 1)


@app.command()
def run(
    ctx: typer.Context,
    step: Annotated[str, typer.Option(help="Stage to run: ingest, parse, or a stage of --recipe.")],
    workspace: Annotated[
        Path | None, typer.Option(envvar="COMMISSURE_WORKSPACE", help="Workspace directory.")
    ] = None,
    recipe: Annotated[
        Path | None,
        typer.Option(envvar="COMMISSURE_RECIPE", help="Recipe folder (recipe stages)."),
    ] = None,
    source: Annotated[
        Path | None, typer.Option(help="Source folder to ingest (step ingest).")
    ] = None,
    role: Annotated[
        str, typer.Option(help="Role recorded on ingested items (step ingest).")
    ] = "corpus",
    ignore: Annotated[
        list[str] | None,
        typer.Option(
            "--ignore", help="Additional filename or locator glob to ignore (step ingest)."
        ),
    ] = None,
    no_default_ignore: Annotated[
        bool,
        typer.Option(
            "--no-default-ignore",
            help="Do not apply the default folder ignore list (step ingest).",
        ),
    ] = False,
    parser: Annotated[
        str | None,
        typer.Option(
            help=f"Parser to run (step parse): {', '.join(PARSERS)}. "
            "Default: the default parser for each item's type."
        ),
    ] = None,
    param: Annotated[
        list[str] | None,
        typer.Option("--param", help="Override a stage parameter as KEY=VALUE (recipe stages)."),
    ] = None,
    trigger: Annotated[
        str, typer.Option(help="What started this run (manual, cron, ...).")
    ] = "manual",
    limit: Annotated[
        int | None, typer.Option(min=1, help="Process at most N pending subjects.")
    ] = None,
    workers: Annotated[
        int, typer.Option(min=1, help="Concurrent subjects (parse and recipe stages).")
    ] = DEFAULT_WORKERS,
    fresh: Annotated[
        bool, typer.Option(help="Do not resume an unfinished run (recipe stages).")
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Bypass LLM cache reads while still writing responses (recipe stages).",
        ),
    ] = False,
    allow_empty: Annotated[
        bool, typer.Option(help="Permit a run that discovers no items.")
    ] = False,
    progress: Annotated[
        bool | None,
        typer.Option(
            "--progress/--no-progress",
            help="Progress lines on stderr (default: only when stderr is a terminal).",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Run one stage against a workspace."""
    kind = step if step in CORE_STEPS else "recipe"
    if not role.strip() or not trigger.strip():
        _fail_usage("--role and --trigger must not be empty")
    explicit = {
        name
        for name in (
            "recipe",
            "source",
            "role",
            "ignore",
            "no_default_ignore",
            "parser",
            "param",
            "limit",
            "workers",
            "fresh",
            "no_cache",
        )
        if ctx.get_parameter_source(name).name == "COMMANDLINE"
    }
    if explicit - ALLOWED[kind]:
        what = step if kind != "recipe" else "a recipe stage"
        opts = [n.replace("_", "-") for n in sorted(explicit - ALLOWED[kind])]
        _fail_usage(f"--{', --'.join(opts)} not used by {what}")
    solution_defaults: set[str] = set()
    if workspace is None and recipe is None:
        cwd = Path.cwd()
        if (cwd / "recipe").is_dir():
            workspace = (cwd / "workspace").resolve()
            solution_defaults.add("workspace")
            if kind == "recipe":
                recipe = (cwd / "recipe").resolve()
                solution_defaults.add("recipe")
    if workspace is None:
        _fail_usage("--workspace is required (or run from a solution directory containing recipe/)")
    param_overrides = _parse_params(param) if kind == "recipe" else {}
    if step == "ingest" and source is None:
        _fail_usage("--source is required for --step ingest")
    if parser is not None and parser not in PARSERS:
        _fail_usage(f"unknown parser {parser!r}; choose from {', '.join(PARSERS)}")
    if parser is not None:
        try:
            PARSERS[parser].check()
        except ParserUnavailable as exc:
            _fail_usage(str(exc))
    loaded = None
    recipe_limits = None
    llm_target = None
    if kind == "recipe":
        if recipe is None:
            _fail_usage(f"--step {step} is not a core step; pass --recipe to run a recipe stage")
        try:
            loaded = load_recipe(recipe)
        except RecipeError as exc:
            _fail_usage(str(exc))
        if step not in loaded.stages:
            _fail_usage(
                f"recipe {loaded.name} has no stage {step!r}; it has {', '.join(loaded.stages)}"
            )
        try:
            llm_target = validate_stage_llm({**loaded.stages[step].params, **param_overrides})
        except ValueError as exc:
            _fail_usage(str(exc))
        recipe_limits = loaded.limits
    else:
        try:
            if recipe is not None:
                recipe_limits = load_recipe_limits(recipe)
            elif (
                "workspace" in solution_defaults and (Path.cwd() / "recipe" / "recipe.py").is_file()
            ):
                recipe_limits = load_recipe_limits((Path.cwd() / "recipe").resolve())
        except RecipeError as exc:
            _fail_usage(str(exc))

    try:
        limits, limits_cfg = resolve_limits(recipe_limits)
    except ValueError as exc:
        _fail_usage(str(exc))

    # Local model servers (`llama.cpp`, `ollama`, localhost OpenAI endpoints) serve a single
    # GPU/CPU inference queue; concurrent long-context requests wait behind each other and
    # trip client `timeout_s`. Default local-LLM stages to `workers=1` unless `--workers` was
    # explicitly passed on the command line.
    local_llm_default_workers = (
        llm_target is not None and llm_target.is_local and "workers" not in explicit
    )
    if local_llm_default_workers:
        workers = 1

    config = _resolved_config(ctx)
    if local_llm_default_workers:
        config["workers"] = {"value": 1, "from": "local-llm-default"}
    for name in solution_defaults:
        resolved = workspace if name == "workspace" else recipe
        config[name] = {"value": str(resolved), "from": "solution-dir"}
    resolved_ignore = effective_ignore(ignore, no_default_ignore=no_default_ignore)
    if step == "ingest":
        config["effective_ignore"] = list(resolved_ignore)
    if loaded is not None:
        config["recipe_fp"] = loaded.fingerprint
    config["limits"] = limits_cfg
    # Progress goes to stderr so `--json` stdout and exit codes stay unchanged. Off by
    # default when stderr is not a terminal: cron mails any output, and a scheduled run
    # that used to be quiet must stay quiet.
    show_progress = progress if progress is not None else sys.stderr.isatty()
    reporter = ProgressReporter(lambda line: typer.echo(line, err=True)) if show_progress else None
    try:
        if step == "ingest":
            check_folder_source(source, workspace)
        with lock(workspace):
            ws = open_workspace(workspace)
            try:
                reap_abandoned(ws.conn)
                if step == "ingest":
                    result = ingest_folder(
                        ws,
                        source,
                        role=role,
                        trigger=trigger,
                        config=config,
                        allow_empty=allow_empty,
                        ignore=resolved_ignore,
                        limits=limits,
                    )
                else:
                    if loaded is not None:
                        effective = {**loaded.stages[step].params, **param_overrides}
                        register_stages(ws.conn, loaded, param_overrides={step: effective})
                        plan = recipe_plan(
                            ws, loaded, step, allow_empty=allow_empty, params=param_overrides
                        )
                    else:
                        plan = parse_plan(
                            ws, parser, allow_empty=allow_empty, limits=limits, workers=workers
                        )
                    result = run_plan(
                        ws,
                        plan,
                        trigger=trigger,
                        config=config,
                        limit=limit,
                        workers=workers,
                        fresh=fresh,
                        no_cache=no_cache,
                        on_progress=reporter.report if reporter is not None else None,
                    )
                    if reporter is not None:
                        reporter.finish()
            finally:
                ws.close()
    except WorkspaceLocked as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_LOCKED) from None
    except (WorkspaceError, ValueError) as exc:
        _fail_usage(str(exc))

    report = {"step": step, "workspace": str(ws.root), **result.as_dict()}
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            if key not in ("step", "workspace") and value is not None:
                typer.echo(f"{key:>14}: {value}")
    raise typer.Exit(EXIT_FAILED if result.status == "failed" else EXIT_OK)


@app.command()
def init(
    path: Annotated[
        Path,
        typer.Argument(
            help="Solution directory to create. May already exist, but must hold no scaffold files."
        ),
    ],
    name: Annotated[
        str | None,
        typer.Option(help="Solution name used in the files; defaults to the folder name."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Create a solution directory from the built-in template. Creates no workspace."""
    try:
        written = scaffold_solution(path, name)
    except ScaffoldError as exc:
        _fail_usage(str(exc))
    report = {
        "solution": str(path.resolve()),
        "name": name or path.resolve().name,
        "created": [p.as_posix() for p in written],
    }
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        typer.echo(f"created solution {report['name']} in {report['solution']}")
        for rel in report["created"]:
            typer.echo(f"  {rel}")
        typer.echo(
            "next: add intake/<batch>/ and fill docs/notes.md, then investigate (see README.md)"
        )
    raise typer.Exit(EXIT_OK)


@app.command()
def serve(
    workspace: Annotated[
        Path | None,
        typer.Option(envvar="COMMISSURE_WORKSPACE", help="Workspace directory."),
    ] = None,
    recipe: Annotated[
        Path | None,
        typer.Option(
            envvar="COMMISSURE_RECIPE",
            help="Recipe folder defining INTERFACE in recipe.py.",
        ),
    ] = None,
    host: Annotated[
        str,
        typer.Option(help="Local address to bind."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(min=0, max=65535, help="Local TCP port to bind (0 picks a free port)."),
    ] = 8000,
) -> None:
    """Serve a recipe's local web interface (requires INTERFACE in recipe.py)."""
    # Match `run`: apply solution-directory defaults only when neither path was supplied,
    # so passing --recipe A/recipe from inside solution B never binds B/workspace by mistake.
    if workspace is None and recipe is None:
        cwd = Path.cwd()
        if (cwd / "recipe").is_dir():
            workspace = (cwd / "workspace").resolve()
            recipe = (cwd / "recipe").resolve()
    if workspace is None:
        _fail_usage(
            "--workspace is required (or set COMMISSURE_WORKSPACE, or run from a solution"
            " directory containing recipe/)"
        )
    if recipe is None:
        _fail_usage(
            "--recipe is required (or set COMMISSURE_RECIPE, or run from a solution"
            " directory containing recipe/)"
        )
    try:
        ui_app = InterfaceApp(workspace, recipe)
    except (RecipeError, WorkspaceError) as exc:
        _fail_usage(str(exc))

    try:
        server = make_server(ui_app, host=host, port=port)
    except OSError as exc:
        _fail_usage(f"could not bind {host}:{port}: {exc}")

    bound_host, bound_port = server.server_address[:2]
    display_host = f"[{bound_host}]" if ":" in str(bound_host) else str(bound_host)
    typer.echo(f"serving {recipe.resolve().name} interface at http://{display_host}:{bound_port}/")
    typer.echo(f"workspace: {ui_app.workspace_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ui_app.shutdown()
    raise typer.Exit(EXIT_OK)


# --- review --------------------------------------------------------------------------

review_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Sample results at random, record verdicts, and report error rates.",
)
app.add_typer(review_app, name="review")

ReviewWorkspace = Annotated[
    Path | None,
    typer.Option(envvar="COMMISSURE_WORKSPACE", help="Workspace directory."),
]
ReviewRecipe = Annotated[
    Path | None,
    typer.Option(
        envvar="COMMISSURE_RECIPE",
        help="Recipe folder whose stages declare checks (default: ./recipe in a solution"
        " directory; parse checks need none).",
    ),
]
Reviewer = Annotated[
    str | None,
    typer.Option(envvar="COMMISSURE_REVIEWER", help="Who is reviewing (required for writes)."),
]
AsJson = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _review_open(workspace: Path | None, recipe: Path | None):
    """The review connection (no lock, no migration) and the recipe, if any."""
    root = _search_root(workspace).resolve()
    if recipe is None and (Path.cwd() / "recipe" / "recipe.py").is_file():
        recipe = Path.cwd() / "recipe"
    try:
        loaded = load_recipe(recipe) if recipe is not None else None
        conn = review.open_for_review(root)
    except (RecipeError, WorkspaceError) as exc:
        _fail_usage(str(exc))
    return root, conn, loaded


def _review_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (review.ReviewError, WorkspaceError) as exc:
        _fail_usage(str(exc))


@review_app.command("checks")
def review_checks(
    workspace: ReviewWorkspace = None, recipe: ReviewRecipe = None, as_json: AsJson = False
) -> None:
    """List every check with its question and current population. Writes nothing."""
    _, conn, loaded = _review_open(workspace, recipe)
    try:
        rows = list(review.iter_checks_with_population(conn, loaded))
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps({"checks": rows}, indent=2))
    else:
        review.render_checks(Console(), rows)
    raise typer.Exit(EXIT_OK)


@review_app.command("sample")
def review_sample(
    check: Annotated[str, typer.Option(help="Check id, e.g. 'stance:/quotes/*'.")],
    n: Annotated[int, typer.Option("--n", min=1, help="Instances to draw.")],
    seed: Annotated[
        int | None, typer.Option(help="Random seed (chosen and recorded if omitted).")
    ] = None,
    workspace: ReviewWorkspace = None,
    recipe: ReviewRecipe = None,
    reviewer: Reviewer = None,
    as_json: AsJson = False,
) -> None:
    """Draw and record a seeded random sample of a check's instances."""
    _, conn, loaded = _review_open(workspace, recipe)
    try:
        out = _review_call(review.sample, conn, loaded, check, n, reviewer=reviewer, seed=seed)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(out, indent=2))
    else:
        every = " (every instance)" if out["census"] else ""
        typer.echo(
            f"sample {out['sample_id']}: {out['n']} of {out['population']} instances of"
            f" {out['check_id']}{every}, seed {out['seed']}"
        )
    raise typer.Exit(EXIT_OK)


@review_app.command("next")
def review_next(
    sample: Annotated[int | None, typer.Option(help="Only this sample.")] = None,
    instance: Annotated[int | None, typer.Option(help="Show this instance instead.")] = None,
    workspace: ReviewWorkspace = None,
    recipe: ReviewRecipe = None,
    as_json: AsJson = False,
) -> None:
    """Show the next sampled instance without a verdict, with what to check it against."""
    root, conn, loaded = _review_open(workspace, recipe)
    try:
        target = instance if instance is not None else _review_call(review.pending, conn, sample)
        detail = (
            _review_call(review.describe, conn, root, target, loaded)
            if target is not None
            else None
        )
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps({"instance": detail}, indent=2))
    elif detail is None:
        typer.echo("no pending instances")
    else:
        review.render_instance(Console(), detail)
    raise typer.Exit(EXIT_OK)


@review_app.command("record")
def review_record(
    instance: Annotated[int, typer.Argument(help="Review instance id.")],
    answer: Annotated[str, typer.Argument(help="correct, wrong, or unclear.")],
    note: Annotated[str | None, typer.Option(help="Why, in a sentence.")] = None,
    workspace: ReviewWorkspace = None,
    reviewer: Reviewer = None,
    as_json: AsJson = False,
) -> None:
    """Record a verdict. Verdicts are append-only; the latest on an instance counts."""
    _, conn, _ = _review_open(workspace, None)
    try:
        verdict_id = _review_call(
            review.record, conn, instance, answer, reviewer=reviewer, note=note
        )
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps({"verdict_id": verdict_id, "instance_id": instance}, indent=2))
    else:
        typer.echo(f"verdict {verdict_id}: instance {instance} {answer}")
    raise typer.Exit(EXIT_OK)


@review_app.command("spot")
def review_spot(
    check: Annotated[str, typer.Option(help="Check id.")],
    subject: Annotated[str, typer.Option(help="Subject key or locator.")],
    pointer: Annotated[str | None, typer.Option(help="Payload pointer (recipe checks).")] = None,
    page: Annotated[int | None, typer.Option(help="Page (parse checks).")] = None,
    workspace: ReviewWorkspace = None,
    recipe: ReviewRecipe = None,
    as_json: AsJson = False,
) -> None:
    """Record an ad hoc instance; its verdicts are reported but never counted in a rate."""
    root, conn, loaded = _review_open(workspace, recipe)
    try:
        keys = _review_call(_resolve_subject_keys, conn, subject)
        instance_id = _review_call(
            review.spot, conn, loaded, check, keys, pointer=pointer, page=page
        )
        detail = review.describe(conn, root, instance_id, loaded)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps({"instance": detail}, indent=2))
    else:
        review.render_instance(Console(), detail)
    raise typer.Exit(EXIT_OK)


@review_app.command("report")
def review_report(workspace: ReviewWorkspace = None, as_json: AsJson = False) -> None:
    """Per sample: verdict counts, error rate, and its Wilson 95% interval. Writes nothing."""
    _, conn, _ = _review_open(workspace, None)
    try:
        rep = review.report(conn)
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(rep, indent=2))
    else:
        review.render_report(Console(), rep)
    raise typer.Exit(EXIT_OK)


# --- release -------------------------------------------------------------------------

release_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Build and publish single-solution client releases.",
)
app.add_typer(release_app, name="release")


@release_app.command("build")
def release_build(
    target: Annotated[str, typer.Argument(help="Target name, or <solution>/<target>.")],
    skip_tests: Annotated[
        bool, typer.Option("--skip-tests", help="Skip running pytest before building.")
    ] = False,
    offline: Annotated[
        bool, typer.Option("--offline", help="Lock from the uv cache only.")
    ] = False,
    allow_dirty: Annotated[
        bool, typer.Option("--allow-dirty", help="Release uncommitted changes.")
    ] = False,
    as_json: AsJson = False,
) -> None:
    """Build the release of a target into build/releases/<target>/."""
    root, default_sol = release.resolve_root()
    spec = f"{default_sol}/{target}" if default_sol and "/" not in target else target
    try:
        dest, version = release.build(
            root,
            spec,
            run_tests=not skip_tests,
            offline=offline,
            allow_dirty=allow_dirty,
        )
    except release.ReleaseError as exc:
        typer.echo(f"release: {exc}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if as_json:
        typer.echo(json.dumps({"target": target, "version": version, "dest": str(dest)}, indent=2))
    else:
        typer.echo(f"built {target} v{version} in {dest}")
    raise typer.Exit(EXIT_OK)


@release_app.command("publish")
def release_publish(
    target: Annotated[str, typer.Argument(help="Target name, or <solution>/<target>.")],
    tarball: Annotated[
        bool,
        typer.Option(
            "--tarball", help="Write solutions/<solution>/releases/<target>-v<version>.tar.gz."
        ),
    ] = False,
    push: Annotated[
        bool, typer.Option("--push", help="Push the built release to the target's remote.")
    ] = False,
    as_json: AsJson = False,
) -> None:
    """Publish the latest built release of a target."""
    root, default_sol = release.resolve_root()
    spec = f"{default_sol}/{target}" if default_sol and "/" not in target else target
    try:
        done = release.publish(root, spec, tarball=tarball, push=push)
    except release.ReleaseError as exc:
        typer.echo(f"release: {exc}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if as_json:
        typer.echo(json.dumps({"target": target, "published": done}, indent=2))
    else:
        for line in done:
            typer.echo(line)
    raise typer.Exit(EXIT_OK)


# --- licenses ------------------------------------------------------------------------


@app.command("licenses")
def licenses_cmd(as_json: AsJson = False) -> None:
    """List third-party components and their licenses; exit 1 if any breaks the policy."""
    report = licenses.build_report()
    if as_json:
        typer.echo(json.dumps(report.to_json(), indent=2))
    else:
        console = Console()
        table = Table("Component", "Version", "License", "Scope", "Status")
        for c in report.components:
            table.add_row(
                escape(c.name), c.version or "", escape(c.license or c.declared or ""),
                c.scope, c.status,
            )  # fmt: skip
        console.print(table)
        console.print(f"Allowed: {', '.join(report.allowed)}")
        for c in report.components:
            if c.status != "allowed" or any("bundles" in n for n in c.notes):
                for note in c.notes:
                    console.print(f"[bold]{escape(c.name)}[/]: {escape(note)}")
        for problem in report.problems:
            typer.echo(f"licenses: {problem}", err=True)
    raise typer.Exit(EXIT_FAILED if report.problems else EXIT_OK)
