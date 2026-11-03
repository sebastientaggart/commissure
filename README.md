<p align="center">
  <img src="docs/logo/logo-stacked.svg" alt="Commissure" width="280">
</p>

<p align="center">
  <a href="pyproject.toml"><img src="docs/logo/badge-python.svg" alt="python: 3.12+"></a> 
  <a href="commissure/workspace.py"><img src="docs/logo/badge-storage.svg" alt="storage: SQLite WAL · FTS5"></a> 
  <a href="commissure/records.py"><img src="docs/logo/badge-schemas.svg" alt="schemas: Pydantic v2"></a> 
  <a href="AGENTS.md"><img src="docs/logo/badge-egress.svg" alt="egress: offline by default"></a> 
  <a href="LICENSE"><img src="docs/logo/badge-license.svg" alt="license: MIT"></a> 
</p>

**Commissure** is a local-first Python substrate and workflow discipline for coding agents—and the people reviewing their work—operating on messy document corpora and structured data.

Pointing a coding agent at a pile of PDFs, Word documents, and spreadsheets without guardrails produces brittle one-off scripts, silent text truncation, hallucinated fallbacks, and claims nobody can verify against the source. Commissure provides the invariant machinery that keeps document operations honest, resumable, and auditable.

---

## Why Commissure?

- **Read honestly before extracting.** `commissure probe` and `commissure run --step parse` evaluate PDFs, DOCX files, and workbooks structurally—judging PDF readability page by page from font Unicode mappings, glyph decodability, and image coverage rather than character count. Unreadable or scanned pages are quarantined with machine-readable reasons or routed through pinned local Tesseract OCR, never silently guessed or skipped.
- **Deterministic first, LLMs for residue.** Recipes are plain Python modules and strict Pydantic v2 models (`extra="forbid"`). The `ctx.llm` seam is an optional tool reserved for unstructured judgment calls—cached before domain validation, logged in SQLite, and offline by default unless a stage explicitly sets `allow_remote=True`.
- **Every claim points to the original (`Evidence` & `Review`).** Extracted records carry core `Evidence` pointers—verified character spans and page numbers resolved against the exact ingested bytes. `commissure review` (and the sandboxed `/review` browser UI) draws seeded random samples so human reviewers can grade claims side-by-side with the original document and publish statistical error rates (Wilson 95% confidence intervals) directly inside deliverables.
- **One local SQLite workspace, ready to ship.** Each operational cycle lives in a single SQLite database (`state.db` in `WAL` mode) beside sharded content-addressed `blobs/`. Stages checkpoint per subject (`valid`, `warning`, `quarantined`, `skipped`) and automatically detect code, parameter, and input staleness. When a solution is ready for a client, `commissure release` packages a leak-scanned, self-contained appliance repository or tarball.

### What it isn't
- **Not a cloud service, daemon, or scheduler.** One invocation, one machine, one SQLite connection. Unattended runs use OS timers (`cron`, `launchd`, `systemd`) backed by non-blocking lockfiles and deterministic exit codes.
- **Not a YAML DSL or autonomous agent loop.** Recipes are regular Python packages importing from [`commissure.api`](commissure/api.py), with no hidden prompts, no silent `text[:N]` truncation, and no invented fallback values.

---

## A 30-Second Taste

A recipe is a Python package (`solutions/<name>/recipe/`) declaring strict Pydantic schemas and stages over `item`, `group`, or `run` subjects:

```python
from pydantic import BaseModel, ConfigDict
from commissure.api import Check, Diagnostic, Evidence, Group, Item, RecordEnvelope, RunContext, Stage


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    summary: str
    evidence: Evidence


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    total: int


def extract(ctx: RunContext, item: Item) -> Finding | RecordEnvelope[Finding]:
    if not item.text or ":" not in item.text:
        return RecordEnvelope.quarantined(
            "missing_header",
            Diagnostic(code="missing_header", message="Expected 'category: summary'"),
        )
    category, raw = item.text.split(":", 1)
    summary = raw.strip()
    start = item.text.index(summary)
    return Finding(
        category=category.strip(),
        summary=summary,
        evidence=Evidence.from_item(item, kind="header", text=summary, start=start, end=start + len(summary)),
    )


STAGES = [
    Stage(
        "extract",
        extract,
        schema=Finding,
        text="default",
        checks=(Check("/evidence", "Does the extracted summary match the source header?"),),
    ),
    Stage(
        "summarize",
        lambda ctx, group: Summary(category=group.key, total=len(group.items)),
        schema=Summary,
        subject="group",
        needs=("extract",),
        group_by=lambda item: item.upstream["extract"].category,
    ),
]
```

Every CLI command supports `--json` for coding agents:

```sh
# 1. Inspect raw files (stateless)
uv run commissure probe intake/batch-01 --json

# 2. Version & store in blobs/
uv run commissure run --step ingest --source intake/batch-01 --role corpus

# 3. Extract structure & FTS5 text
uv run commissure run --step parse

# 4. Iterate on a 10-item slice
uv run commissure run --step extract --limit 10

# 5. Run stages to completion
uv run commissure run --step extract && uv run commissure run --step summarize

# 6. Browse UI & /review in browser
uv run commissure serve
```

---

## Getting Started

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/):

```sh
brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                  # creates .venv and installs dependencies
uv run commissure --help
```

- **Start a new operation with an agent:** Run `uv run commissure init solutions/<name>` and follow the agent workflow below.
- **Run an existing solution:** Clone the solution repository into `solutions/<name>/` and follow its `README.md`.
- **Contribute or run the test suite:** See [`docs/contributing.md`](docs/contributing.md).

---

## Agent-First Workflow

Commissure ships its engineering rules in [`AGENTS.md`](AGENTS.md) and repeatable agent workflows in [`.agents/skills/`](.agents/skills/):

| Step | Skill / Actor | What Happens |
| :--- | :--- | :--- |
| **1. Scaffold** | `new-solution` | Scaffolds `solutions/<name>/` via `commissure init` and lists what to populate. |
| **2. Populate** | Operator | Drops sample inputs into `intake/<batch>/` and reference notes into `docs/`. |
| **3. Recon** | `investigate-solution` | Probes `intake/`, parses a scratch workspace, runs `commissure recon`, and completes `recipe/AGENTS.md` before any stage code is written. |
| **4. Build** | `build-recipe-stage` | Builds one stage or deliverable at a time with `--limit`, `commissure inspect`, and proven-to-fail tests. |
| **5. Ship** | `release-target` | Runs tests and leak scans via `commissure release build` and publishes client tarballs or git tags. |

---

## Documentation & License

- **[Architecture & Core Concepts](docs/architecture.md)** — Workspaces, source vs. parser drift, readability gates, stage staleness, `Evidence`, and `Review`.
- **[Operator & Recipe Guide](docs/guide.md)** — Solution layout, full CLI reference (`probe`, `run`, `recon`, `inspect`, `search`, `review`, `serve`, `release`), and Tesseract OCR setup.
- **[Contributing & Development](docs/contributing.md)** — Local setup, verification rules, **Rule of Two**, and third-party license register (`uv run commissure licenses`).
- **[`AGENTS.md`](AGENTS.md)** — Authoritative playbook and structural invariants for coding agents.

Licensed under the [MIT License](LICENSE).
