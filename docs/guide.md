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
        evidence=Evidence.span(item, span_start, span_end, kind="summary_span"),
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

### HTML Deliverables & Charts (`render_page`)

Deliverable stages render body fragments through `render_page` ([`commissure/deliver/html.py`](../commissure/deliver/html.py)), which wraps them in the shared offline shell (`pico.min.css`, [`report.css`](../commissure/deliver/static/report.css), [`report.js`](../commissure/deliver/static/report.js), and optional `recipe/brand/brand.css`).

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
- **Chart.js defaults (`scripts=("chart.umd.min.js",)`):** Passing `"chart.umd.min.js"` to `render_page` automatically includes [`charts.js`](../commissure/deliver/static/charts.js) right after it. `charts.js` reads `:root` tokens, sets `Chart.defaults` (page font, axis/grid ink, animation off, `index` hover tooltips without color boxes on single-series charts, 24px bar thickness cap with 4px top radius, 2px round-joined lines), colors datasets from `--chart-series` or `--chart-cat-*` (keyed by `dataset.key` via `CommissureCharts.colorFor(key, keys)`), and registers `CommissureCharts.capLabels` and `CommissureCharts.referenceLines`:
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

Every command supports `--json` for structured agent consumption. `commissure run` writes live progress to `stderr` when it is a terminal (`--progress` / `--no-progress` override), so cron runs stay quiet. When invoked inside `solutions/<name>/`, `--recipe` and `--workspace` default to `./recipe` and `./workspace`.

Deterministic exit codes apply across all commands:
- `0`: Succeeded (or clean `noop` when all items are unchanged)
- `1`: Run or gate failed
- `2`: Usage, configuration, or recipe validation error
- `3`: Workspace is locked by another writer

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

---

## 4. Working with Coding Agents

See [`docs/solutions.md`](solutions.md) for the step-by-step solution lifecycle (`new-solution`, `investigate-solution`, `build-recipe-stage`, `release-target`), operator vs. agent responsibilities, `recipe/tools/` fetchers, `intake/` curation, and roles.

---

## 5. Delivering to Clients (`commissure release`)

Clients who run a solution as an appliance receive a generated single-solution release repository or tarball, never the development repository, `intake/`, `workspace/`, or internal `docs/`:

- `solutions/<name>/release.toml` declares the solution's `version`, allowlisted paths (default `recipe/`, `CHANGELOG.md`, and `docs/user-guide.md` when present, linked from the generated `README.md`), optional extras (`extras = ["docx"]`), and a short quick-start `usage`.
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
