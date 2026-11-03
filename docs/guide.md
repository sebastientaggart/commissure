# Operator & Recipe Guide

This guide covers how to scaffold a solution, write a recipe, run the CLI workflow, inspect and review results, and package client releases.

---

## 1. Solution Layout

Each operation lives in its own git repository under `solutions/<name>/` (gitignored by the core `commissure` repository). Running `uv run commissure init solutions/<name>` scaffolds the directory from [`commissure/scaffold/template/`](../commissure/scaffold/template/):

```text
solutions/<name>/
  README.md       Arrangements for this operation; the populate → investigate → build sequence
  .gitignore      Excludes workspace/, intake documents, and secrets (.env)
  CHANGELOG.md    One entry per released version
  release.toml    Version, allowlist of what ships, client-facing usage
  releases/       Client targets (<target>.toml) and published tarballs (never shipped)
  docs/           Reference documentation and notes, indexed in docs/README.md
  intake/         Input documents, one subfolder per source to register (local; README tracked)
  tests/          Solution tests and fixtures (collected by pytest; never shipped)
  workspace/      state.db, blobs/, outputs/ (created by the first run; local)
  recipe/
    AGENTS.md     Operation purpose, recon notes, domain & quarantine rules, acceptance gate
    recipe.py     Entry point: defines STAGES (ordered list of Stage declarations) and optional INTERFACE
    schema.py     Strict Pydantic payload models (ConfigDict(extra="forbid"))
    extract.py    Per-item extraction → schema instance or RecordEnvelope
    analyze.py    Cross-item group and run stages, entity/edge population
    deliver.py    Renders outputs from workspace state into workspace/outputs/
    tools/        Optional pre-ingest fetchers/scripts that populate intake/ and read .env
```

The scaffolded `recipe/recipe.py` declares no stages and fails at startup on purpose until the operator populates `intake/` and `docs/` and an agent completes reconnaissance in `recipe/AGENTS.md`.

---

## 2. Writing a Recipe

Sibling modules inside `recipe/` import each other relatively (`from .schema import ...`) and import core types exclusively from [`commissure.api`](../commissure/api.py) (plus optional helpers from [`commissure.patterns`](../commissure/patterns/__init__.py)).

```python
# schema.py
from commissure.api import Evidence
from pydantic import BaseModel, ConfigDict


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workstream: str
    summary: str
    evidence: Evidence


class Consolidated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workstream: str
    count: int
```

```python
# extract.py
from commissure.api import Diagnostic, Evidence, Item, RecordEnvelope, RunContext

from .schema import Report


def extract(ctx: RunContext, item: Item) -> Report | RecordEnvelope[Report]:
    if not item.text or ":" not in item.text:
        return RecordEnvelope.quarantined(
            "missing_header",
            Diagnostic(code="missing_header", message="Expected 'workstream: summary'"),
        )
    workstream, summary = item.text.split(":", 1)
    span_start = item.text.index(summary.strip())
    span_end = span_start + len(summary.strip())
    return Report(
        workstream=workstream.strip(),
        summary=summary.strip(),
        evidence=Evidence.from_item(
            item,
            kind="summary_span",
            text=item.text[span_start:span_end],
            start=span_start,
            end=span_end,
        ),
    )
```

```python
# recipe.py
from commissure.api import Check, Group, RunContext, Stage

from .extract import extract
from .schema import Consolidated, Report


def consolidate(ctx: RunContext, group: Group) -> Consolidated:
    return Consolidated(workstream=group.key, count=len(group.items))


STAGES = [
    Stage(
        "extract",
        extract,
        schema=Report,
        text="default",
        checks=(Check("/evidence", "Does the extracted summary match the source header?"),),
    ),
    Stage(
        "consolidate",
        consolidate,
        schema=Consolidated,
        subject="group",
        needs=("extract",),
        group_by=lambda item: item.upstream["extract"].workstream,
    ),
]
```

---

## 3. CLI Workflow & Reference

Every command supports `--json` for structured agent consumption. `commissure run` writes live progress to `stderr` when it is a terminal (`--progress` / `--no-progress` override), so cron runs stay quiet. When invoked inside `solutions/<name>/`, `--recipe` and `--workspace` default to `./recipe` and `./workspace`.

Deterministic exit codes apply across all commands:
- `0` — Succeeded (or clean `noop` when all items are unchanged)
- `1` — Run or gate failed
- `2` — Usage, configuration, or recipe validation error
- `3` — Workspace is locked by another writer

### Step-by-Step Pipeline Execution

```sh
# 0. Scaffold a new solution
uv run commissure init solutions/<name> --json
cd solutions/<name>

# 1. Stateless structural reconnaissance on raw files (requires no workspace)
uv run commissure probe intake/<batch> --json

# 2. Ingest a folder as a source and classify byte drift
uv run commissure run --step ingest --source intake/<batch> --role corpus --json

# 3. Parse current items into structured blocks, tables, and FTS5-indexed text
uv run commissure run --step parse --json
#    Optional: run Tesseract OCR over pages rejected by the readability gate
uv run commissure run --step parse --parser ocr --json

# 4. Measure corpus-wide text patterns (repeated headers/footers, duplicate texts, table headers)
uv run commissure recon --json

# 5. Iterate on a recipe stage over a small slice, then run to completion
uv run commissure run --step extract --limit 10 --json
uv run commissure run --step extract --json
uv run commissure run --step consolidate --json
```

### Inspecting a Workspace (`commissure inspect` & `commissure recon`)
`commissure inspect` opens `state.db` read-only without taking the workspace lock or running migrations, so it can run while `commissure run` is active:

```sh
# High-level workspace summary (sources, roles, runs, staleness, drift, issues, LLM calls)
uv run commissure inspect --json

# Compare multiple workspaces side by side
uv run commissure inspect --workspace workspace-a --workspace workspace-b

# Drill into one subject (by item key, group key, locator, or unique filename)
uv run commissure inspect --subject item:26
uv run commissure inspect --subject "report-2026.pdf"

# Inspect a single cached LLM request and raw response
uv run commissure inspect --call 14
```

`--subject` displays every run that touched the subject, whether the original source file still matches the ingested blob, `#page=N` file links for parse diagnostics, extracted text per parser, current record payloads, and resolved `evidence_review` spans with surrounding context and OCR trust.

`commissure recon` performs read-only corpus measurements over parsed text: locator-parent counts, repeated lines across items, identical `text_hash` sets under one `(parser, parser_version)`, table first-row signatures, unreadable pages, and OCR confidence distributions.

### Searching Parsed Text (`commissure search`)
Finds keyword and phrase hits in current parsed text via the SQLite FTS5 index (read-only, lock-free):

```sh
# Literal phrase search (default)
uv run commissure search "program improvement" --json

# FTS5 boolean query (--raw) or specific parser (--parser)
uv run commissure search "budget AND (personnel OR fringe)" --raw --parser ocr --json
```

Hits return locator, parser, page, and character offsets. Stages can also call `ctx.search(...)` on the main thread.

### Reviewing Results (`commissure review` & `commissure serve`)
Measures empirical accuracy on stage checks and parse page checks (`parse:unreadable_pages`, `parse:ocr_pages`):

```sh
# List declared checks and their current population sizes
uv run commissure review checks

# Draw a seeded random sample of 30 instances from a complete, non-stale stage snapshot
uv run commissure review sample --check extract:/evidence --n 30 --seed 42 --reviewer alice

# Step through pending instances in the terminal
uv run commissure review next
uv run commissure review record 12 correct --reviewer alice --note "Matches table row 3"

# Record an ad-hoc spot check (logged, never counted in sample error rates)
uv run commissure review spot --subject item:26 --check extract:/evidence wrong --reviewer alice

# View sample error rates and Wilson 95% confidence intervals
uv run commissure review report --json
```

To review in the browser or interact with a recipe's local web `INTERFACE`:

```sh
uv run commissure serve --port 8000
```

Opening `/review` in the browser provides the full sampling and grading workflow side-by-side with the sandboxed original document opened at the cited page and character span.

---

## 4. Working with Coding Agents

Commissure ships agent instructions and workflows as versioned files in the repository:
- [`AGENTS.md`](../AGENTS.md) holds the global invariants.
- [`.agents/skills/`](../.agents/skills/) holds four task-specific workflows (with relative symlinks in `.claude/skills/` for Claude Code):

1. **`new-solution`** (agent): Runs `commissure init solutions/<name>`, hands off a checklist of what to populate in `intake/` and `docs/`, and stops.
2. **Operator populates:** Places a representative batch of inputs in `intake/<batch>/` and reference notes in `docs/` (indexed in `docs/README.md`).
3. **`investigate-solution`** (agent): Reads `docs/README.md` and all notes, runs `probe`, ingests and parses into a `/tmp` scratch workspace (including `--parser ocr` when needed), runs `recon`, completes `recipe/AGENTS.md`, and stops for review before writing stage code.
4. **`build-recipe-stage`** (agent): Implements one stage, deliverable, or `INTERFACE` at a time—strict schema first, deterministic extraction second, `ctx.llm` for residue only—iterating with `--limit N --json` and writing proven-to-fail tests in `solutions/<name>/tests/`.

---

## 5. Delivering to Clients (`commissure release`)

Clients who run a solution as an appliance receive a generated single-solution release repository or tarball—never the development repository, `intake/`, `workspace/`, or internal `docs/`:

- `solutions/<name>/release.toml` declares the solution's `version`, allowlisted paths (default `recipe/` and `CHANGELOG.md`), optional extras (`extras = ["docx"]`), and client usage instructions.
- `solutions/<name>/releases/<target>.toml` configures a specific client destination (`title`, optional git `remote`, `tarball = true`, and target-specific `forbid` regexes).

```sh
# After updating a solution: bump version in release.toml, update CHANGELOG.md, and commit
uv run commissure release build <target>              # runs tests, builds wheel, writes build/releases/<target>/
uv run commissure release publish <target> --tarball  # writes solutions/<name>/releases/<target>-v<version>.tar.gz
uv run commissure release publish <target> --push     # pushes tag and branch to the target's client remote
```

Every `release build` refuses on uncommitted changes, failing tests, a `TODO` in `release.toml`, changed solution files without a version bump and `CHANGELOG.md` entry, an already-released version, or any file (including inside the vendored `commissure` wheel) matching built-in credential/path patterns (`DEFAULT_FORBID`), organization-wide patterns in `solutions/.forbid.toml`, another solution's name, or the target's `forbid` list.

The client simply unpacks the tarball (or pulls the release repo) and runs `uv sync` followed by the commands in the generated `README.md`.

---

## 6. Optional Tools: Tesseract (OCR), `docx` Extra & Git LFS

### Pinning Tesseract for OCR
The `ocr` parser drives a locally installed Tesseract binary (Apache-2.0). Install and pin it so routine OS upgrades do not silently change the `ocr` parser version and mark existing OCR workspaces stale:

```sh
brew install tesseract   # Debian/Ubuntu: apt install tesseract-ocr && apt-mark hold tesseract-ocr
brew pin tesseract       # Prevent routine `brew upgrade` from bumping the binary version
tesseract --version      # Recorded in every ocr row, e.g. tesseract=5.5.3
```

To upgrade Tesseract intentionally: `brew unpin tesseract && brew upgrade tesseract && brew pin tesseract`, then re-run `--parser ocr` on solutions that use OCR. Without Tesseract installed, everything else in Commissure works normally and `--parser ocr` exits `2` with an install hint.

### Word Template Deliverables (`commissure[docx]`)
Core `commissure` dependencies are strictly permissive (MIT, BSD, Apache-2.0, ISC). Recipes that render `.docx` templates using `docxtpl` (LGPL-2.1-only) opt in via `extras = ["docx"]` in `release.toml` and `uv sync --extra docx`.

### Git LFS in Solution Repositories
By default, solutions keep `intake/` and `workspace/` local to each machine. A solution repository that opts into tracking its corpus in git uses `.gitattributes` in that solution repo to store binaries in Git LFS (`brew install git-lfs && git lfs install`). The core `commissure` repository does not require Git LFS.
