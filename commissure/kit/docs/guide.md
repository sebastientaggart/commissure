# Operator & Recipe Guide

This guide covers how to start a solution, write a recipe, run the CLI workflow, inspect and review results, and release a solution.

---

## 1. Solution Layout

Each solution is its own project and git repository, depending on the `commissure` package. `uvx commissure new <dir>` creates one (`--example` for a working example over a small corpus):

```text
<solution>/
  pyproject.toml  Name, version, and `commissure~=X.Y.Z`; uv.lock pins the exact version
  AGENTS.md       The solution's contract: purpose, inputs, domain & quarantine rules, acceptance gate (CLAUDE.md imports it)
  .agents/commissure/  Core's rules, these docs, and skills, for the installed version (never edited)
  README.md       What the solution does and how to run it
  .gitignore      Excludes workspace/, intake documents, and secrets (.env)
  CHANGELOG.md    One entry per released version
  docs/           Reference documentation and notes, indexed in docs/README.md
  intake/         Input documents, one subfolder per source to register (local; README tracked)
  tests/          Solution tests and fixtures (uv run pytest)
  workspace/      state.db, blobs/, outputs/ (created by the first run; local)
  recipe/
    recipe.py     Entry point: defines STAGES (ordered list of Stage declarations) and optional INTERFACE
    schema.py     Strict Pydantic payload models (ConfigDict(extra="forbid"))
    extract.py    Per-item extraction → schema instance or RecordEnvelope
    *.py          More stage modules as the recipe grows (group and run stages, deliverables, INTERFACE)
    tools/        Optional pre-ingest fetchers/scripts that populate intake/ and read .env
```

The scaffolded `recipe/recipe.py` declares no stages and fails at startup on purpose until the operator populates `intake/` and `docs/` and an agent completes reconnaissance in the contract (`AGENTS.md`).

---

## 2. Writing a Recipe

Sibling modules inside `recipe/` import each other relatively (`from .schema import ...`) and import core types exclusively from `commissure.api` (plus optional helpers from `commissure.patterns`).

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
        evidence=Evidence.span(item, span_start, span_end, kind="summary_span"),
    )
```

An item stage receives the parsed text as `item.text` and its layout as `item.structure`: `{"blocks": [...], "tables": [...], "metadata": {...}, "issues": [...]}`. Each block has a `kind` (`page`, `heading`, `paragraph`, `table`, `sheet`, `document`) and a half-open `start`/`end` into `item.text`, so a block's offsets are a ready `Evidence.span`; headings carry `level` and `heading_path`, PDF blocks a `page`, and page blocks a readability `status` (`readable`, `unreadable`, `blank`). For page counts and OCR confidence use `commissure.patterns.legibility(item.structure)`.

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

### Group and Run Stages, Deliverables

- **Group stages** (`subject="group"`, `group_by=...`) receive `group.items` for one group key; a **run stage** (`subject="run"`) receives one `Group` over every complete item. Incomplete items (no valid upstream record) land in `group.excluded` with a reason when `group_by` can place them from the locator or another field every item has; a `group_by` that reads upstream payloads can't place them, so they stay unplaced. Name what was excluded in the result rather than dropping it silently.
- `ctx.map(fn, items)` runs per-item work inside a group or run stage on the run's bounded thread pool. Stages return a schema model or a `RecordEnvelope` and never touch SQLite.
- **Deliverables** are ordinary run or group stages: they read upstream payloads in memory, write files into `ctx.outputs` (`<workspace>/outputs/<stage>/<run_id>/`), verify them structurally (exists, non-zero size, expected page/sheet/row counts, no external resource URLs), and return a record describing them. Template and brand files a stage reads are listed in `Stage(..., assets=(...))`, so editing one makes the stage stale.

### HTML Deliverables & Charts (`render_page`)

Deliverable stages render body fragments through `render_page` (`commissure/deliver/html.py`), which wraps them in the shared offline shell (`pico.min.css`, `report.css`, `report.js`, and optional `recipe/brand/brand.css`).

`report.css` provides `:root` chart tokens (overridable in `recipe/brand/brand.css`) and zero-JS HTML chart primitives so recipes never hard-code hex values or repurpose `--status-*` tokens as data series:

- **Tokens:** `--chart-cat-1` to `--chart-cat-8` (OKLCH- and color-blind-validated categorical palette; leading 7 slots pass all-pairs separation) plus `--chart-cat-rest`, `--chart-series` (`var(--brand-primary)`), `--chart-seq-1` to `--chart-seq-5`, `--chart-div-neg` / `--chart-div-mid` / `--chart-div-pos`, `--chart-wash`, `--chart-band`, `--chart-grid`, `--chart-axis-ink`, `--chart-label-on-dark`, and `--chart-label-on-light`.
- **HTML primitives (no JavaScript required):**
  - 100% segmented bar and legend:
    ```html
    <div class="chart-segbar" role="img" aria-label="Category split">
      <span class="chart-seg chart-on-dark" style="flex-grow: 48; background: var(--chart-cat-1);">48%</span>
      <span class="chart-seg chart-on-light" style="flex-grow: 22; background: var(--chart-cat-2);">22%</span>
      <span class="chart-seg chart-on-dark" style="flex-grow: 18; background: var(--chart-cat-3);">18%</span>
      <span class="chart-seg chart-on-light" style="flex-grow: 12; background: var(--chart-cat-4);">12%</span>
    </div>
    <ul class="chart-legend">
      <li><span class="chart-swatch" style="background: var(--chart-cat-1);"></span>Category A</li>
      <li><span class="chart-swatch" style="background: var(--chart-cat-2);"></span>Category B</li>
      <li><span class="chart-swatch" style="background: var(--chart-cat-3);"></span>Category C</li>
      <li><span class="chart-swatch" style="background: var(--chart-cat-4);"></span>Category D</li>
    </ul>
    ```
  - Ranked horizontal bar list: `.chart-bars > li` containing `.chart-bar-label`, `.chart-bar-track > .chart-bar`, and `.chart-bar-value`.
- **Chart.js defaults (`scripts=("chart.umd.min.js",)`):** Passing `"chart.umd.min.js"` to `render_page` automatically includes `charts.js` right after it. `charts.js` reads `:root` tokens, sets `Chart.defaults` (page font, axis/grid ink, animation off, `index` hover tooltips without color boxes on single-series charts, 24px bar thickness cap with 4px top radius, 2px round-joined lines), colors datasets from `--chart-series` or `--chart-cat-*` (keyed by `dataset.key` via `CommissureCharts.colorFor(key, keys)`), and registers `CommissureCharts.capLabels` and `CommissureCharts.referenceLines`:
  ```html
  <div class="chart-canvas"><canvas id="daily-counts"></canvas></div>
  <div class="chart-canvas"><canvas id="cumulative-share"></canvas></div>
  <script>
    new Chart(document.getElementById("daily-counts"), {
      type: "bar",
      data: { labels: days, datasets: [{ label: "Count", data: counts }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false }, capLabels: true } }
    });
    new Chart(document.getElementById("cumulative-share"), {
      type: "line",
      data: { labels: days, datasets: [{ label: "Share", data: shares, fill: "origin" }] },
      options: {
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          referenceLines: { bands: [{ start: "2026-05-01", end: "2026-05-15", label: "Closes May 15" }] }
        }
      }
    });
  </script>
  ```

---

## 3. CLI Workflow & Reference

Every command supports `--json` for structured agent consumption. `commissure run` writes live progress to `stderr` when it is a terminal (`--progress` / `--no-progress` override), so cron runs stay quiet. When invoked in a solution folder, `--recipe` and `--workspace` default to `./recipe` and `./workspace`.

Deterministic exit codes apply across all commands:
- `0`: Succeeded (or clean `noop` when all items are unchanged)
- `1`: Run or gate failed
- `2`: Usage, configuration, or recipe validation error
- `3`: Workspace is locked by another writer

### Step-by-Step Pipeline Execution

```sh
# 0. Start a new solution
uvx commissure new <dir> --json
cd <dir> && uv sync

# 1. Stateless structural reconnaissance on raw files (requires no workspace)
uv run commissure probe intake/<batch> --json

# 2. Ingest a folder as a source and classify byte drift
#    (Sources under the solution folder are stored relatively as `intake/<batch>`;
#     outside sources are stored as absolute paths.)
uv run commissure run --step ingest --source intake/<batch> --role corpus --json

#    If an outside source folder (or a workspace created with absolute source paths) moves,
#    rewrite stored folder paths in place while keeping source IDs, items, and verdicts:
uv run commissure sources relocate /old/path /new/path --dry-run --json
uv run commissure sources relocate /old/path /new/path --json
#    If an ingest already ran at the new path and created a duplicate source, retire the
#    unreviewed duplicate (tombstoning its current items and setting sources.retired_at),
#    then relocate the original source:
uv run commissure sources retire <duplicate-source-id> --dry-run --json
uv run commissure sources retire <duplicate-source-id> --json

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

### Pruning Superseded Parses (`commissure prune`)
Because a parser's version includes a hash of its source code and library versions, upgrading core or editing a parser causes the next `commissure run --step parse` to store new `extracted_text` rows beside the old ones. To clear superseded `text` and `structure` bodies that are not referenced by any `evidence` row while preserving `parser_version`, `text_hash`, drift classification, and stage staleness:

```sh
# 1. Back up workspace/state.db first (see "Keeping Verdicts" below)
# 2. Preview rows and bytes that would be cleared, by parser
uv run commissure prune --dry-run --json

# 3. Clear superseded unreferenced bodies and compact state.db with VACUUM
#    (--vacuum requires free disk space roughly equal to state.db)
uv run commissure prune --vacuum --json
```

Run `commissure prune` after parser changes or a core upgrade that re-parsed the corpus. Every newest row per `(item_version_id, parser)` and every row named by `evidence` is kept intact, so `current_text` and all reviewed evidence spans continue to resolve. Without `--vacuum`, `commissure prune` frees pages inside `state.db` and reports how many bytes `VACUUM` would reclaim.

### Entities and Edges (graph)
A stage emits graph rows beside its payload; core indexes them against the outcome, so only the current snapshot shows and a rerun supersedes it:

```python
return RecordEnvelope.valid(
    payload,
    entities=[EntitySpec(kind="topic", key="budget")],
    edges=[
        EdgeSpec(kind="about", source_item_key=item.key, target_entity_key=("topic", "budget")),
        EdgeSpec(kind="cites", source_item_key=item.key, target_unresolved="ref 12(b)"),
    ],
)
```

An entity endpoint must name an entity in the same envelope; provenance (`item_version_ids`) must name item versions the subject owns: item stages default to their own, and a group or run stage must name at least one member on every entity and edge. Any of these mistakes fails the run. Read the current graph with `ctx.entities(...)` / `ctx.edges(...)` (main thread), or `view.entities(...)`, `view.edges(...)`, and `view.search(...)` in an `INTERFACE` render or action. `commissure inspect` summarizes current entities and edges per stage and kind.

When the graph is about which authorities are still in effect (instruments that supersede, stay, vacate, or amend each other), use `commissure.patterns.currency` rather than writing the rules again: `resolve_currency` (the latest dated action decides, an undated one never overrides a dated one, a same-day tie goes to the most final verb and is flagged `same_day_actions`, a freshness stamp never outranks an action), `supersession_date`, `merge_claims` (one edge per source, kind, and target, never without evidence), `lineage` and `deciding_actors` (a citation already dead at issuance, dead only after, or undetermined when a date is unknown; never for a document that is itself a deciding actor; with the freshness-stamp trap only where it can mislead), and `graph_rows` (the same graph as `EntitySpec` / `EdgeSpec` rows with provenance). The recipe keeps its own identities, payload schemas, and domain exemptions. `commissure.patterns.legibility(item.structure)` gives an item's page, OCR-page, and unreadable-page counts and mean OCR confidence without reading the structure format directly.

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
uv run commissure review spot --subject item:26 --check extract:/evidence --pointer /evidence

# View sample error rates and Wilson 95% confidence intervals
uv run commissure review report --json
```

To review in the browser or interact with a recipe's local web `INTERFACE`:

```sh
uv run commissure serve --port 8000
```

Opening `/review` in the browser provides the full sampling and grading workflow side-by-side with the sandboxed original document opened at the cited page and character span.

### Keeping Verdicts

Verdicts are the one original in a workspace. Everything else is rebuilt from `intake/` and the recipe, but samples and verdicts are human answers, and they live only in `workspace/state.db`. A rebuilt workspace starts with an empty verdict log. Commissure does not back them up, for the same reason it does not schedule runs: backups belong to the system owner's tools.

**Back up `state.db` with SQLite's own backup.** It produces a consistent copy even while `run` or `review` is writing:

```sh
sqlite3 workspace/state.db ".backup '/path/to/backups/state-$(date +%F).db'"
# or, compacted (refuses to overwrite an existing file):
sqlite3 workspace/state.db "VACUUM INTO '/path/to/backups/state-$(date +%F).db'"
```

Never copy a live `state.db` with `cp` or a file-sync tool: committed changes can still sit in `state.db-wal`, and a copy without it is behind or corrupt. Point file-level backup tools (Time Machine, restic, rsync) at the `.backup` output, not at the live database, or use a SQLite replicator such as Litestream for continuous copies. `state.db` alone keeps every verdict; for a full restore without re-ingesting, back up the whole workspace the same way (`blobs/` holds the ingested bytes the review page shows).

To restore, stop `run` and `serve`, move the old `state.db`, `state.db-wal`, and `state.db-shm` aside, and copy the backup in as `workspace/state.db`. A leftover `-wal` file beside a restored database can corrupt it.

**Keep a text record in git (optional).** The verdict log is small even when `state.db` is gigabytes, and a dump of it is a readable, diffable record of who judged what:

```sh
sqlite3 workspace/state.db ".dump review_samples review_instances verdicts" > review/verdicts.sql
```

The tables are append-only and dump in row order, so each commit's diff is exactly the new samples and verdicts. It contains reviewer names and notes, and a solution's repository is its release, so commit it only where the client may see them. It is a record, not a restore path: its rows refer to this workspace's outcome ids and `item:<id>` subject keys, which a rebuilt workspace assigns differently, and it carries no append-only triggers. Never load it into another workspace; restore from a database backup.

### Testing a Recipe (`commissure.testing`)

Commissure registers a `pytest` plugin (`commissure.testing`) that provides the `commissure_run` fixture (an instance of `commissure.testing.Pipeline` bound to `tmp_path / "workspace"` and the solution's `recipe/`). Use it instead of wrapping `CliRunner` or querying `current_outcomes` / `current_records` in raw SQL:

```python
def test_extract_and_tally(commissure_run):
    # Folders outside tmp_path (such as intake/<batch>) are copied into tmp_path first.
    assert commissure_run.ingest("intake/comments", role="comment").exit_code == 0
    for report in commissure_run.steps("parse", "extract", "tally").values():
        assert report.exit_code == 0, report

    # Current outcomes per stage, keyed by locator (item), group key (group), or "run" (run):
    # each entry has .outcome, .reason, .codes, and .payload (also accessible as dict keys).
    extract = commissure_run.records("extract")
    assert extract["c09.html"].outcome == "warning"
    assert extract["c09.html"].reason == "no_position_phrase"
    assert extract["c10.html"].outcome == "quarantined"
    assert extract["c23-scan.pdf"].outcome == "skipped"  # quarantined at parse -> no_text

    tally = commissure_run.records("tally")["run"].payload
    assert tally["comments"] == 21
```

For module-scoped pipelines (`tmp_path_factory`), construct `Pipeline(tmp / "workspace", ROOT / "recipe")` directly (`from commissure.testing import Pipeline`). Methods: `.load()`, `.ingest(source, role="corpus", *flags)`, `.parse(parser=None, *flags)`, `.stage(name, *flags)`, `.steps(*names)`, `.records(stage)`, `.review(subcommand, *args, reviewer=None)`, and `.workspace`. None of the runner methods assert on `.exit_code`, so tests can assert failure exit codes (`1`, `2`, `3`) directly.

---

## 4. Working with Coding Agents

See [`solutions.md`](solutions.md) for the step-by-step solution lifecycle (`commissure new`, `investigate-solution`, `build-recipe-stage`, upgrading core, releasing), operator vs. agent responsibilities, `recipe/tools/` fetchers, `intake/` curation, and roles.

---

## 5. Releasing a Solution

A solution's repository at a tag is its release: the client clones it (or receives `git archive` of the tag) and runs `uv sync`, then the commands in its `README.md`. `uv.lock` pins the exact `commissure` version, installed from PyPI.

```sh
# bump version in pyproject.toml, add its CHANGELOG.md entry, then:
uv run pytest
git commit -am "Release 0.2.0" && git tag v0.2.0
```

`intake/` and `workspace/` are never tracked, so client data never ships. Files that should stay home (internal notes) are left out of an archive with `export-ignore` in `.gitattributes`, or kept out of the repository.

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
Core `commissure` dependencies are strictly permissive (MIT, BSD, Apache-2.0, ISC). Recipes that render `.docx` templates using `docxtpl` (LGPL-2.1-only) opt in by depending on `commissure[docx]` instead of `commissure` in the solution's `pyproject.toml`.

### Git LFS in Solution Repositories
By default, solutions keep `intake/` and `workspace/` local to each machine. A solution repository that opts into tracking its corpus in git uses `.gitattributes` in that solution repo to store binaries in Git LFS (`brew install git-lfs && git lfs install`). The core `commissure` repository does not require Git LFS.
