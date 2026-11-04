<p align="center">
  <img src="docs/logo/logo-stacked.svg" alt="Commissure" width="280">
</p>

<p align="center">
  <a href="pyproject.toml"><img src="docs/logo/badge-python.svg" alt="python: 3.12+"></a>
  <a href="docs/architecture.md"><img src="docs/logo/badge-pydantic.svg" alt="pydantic: v2"></a>
  <a href="docs/architecture.md"><img src="docs/logo/badge-storage.svg" alt="storage: SQLite"></a>
  <a href="pyproject.toml"><img src="docs/logo/badge-uv.svg" alt="uv: managed"></a>
  <a href="LICENSE"><img src="docs/logo/badge-license.svg" alt="license: MIT"></a>
</p>

**Commissure** helps a small team do document work that used to take a large team. Point it at thousands of PDFs, Word files, spreadsheets, or downloaded records. It reads what can be read, flags what can't, runs the same complex analysis across every item, and keeps a pointer from every result back to the page it came from. People still check the work, but Commissure makes checking possible.

It runs on one machine, keeps your data there, and is free to use under the MIT license.

<sub>Named for the commissure, the tissue that connects the two halves of the brain.</sub>

---

## The problem

Offices are being asked to do the same review, analysis, and reporting with a fraction of the people. The documents didn't get fewer. The deadlines didn't move. And the analysts who left took with them the memory of everything the office had already read, decided, and said.

The usual options don't fit:

- **A person and a spreadsheet** is accurate but slow, and the whole exercise is repeated the next time the documents change.
- **An AI chat tool** is fast but handles one document at a time, guesses when it can't read something, and can't show where an answer came from.
- **A one-off script** written by a developer (or a coding agent) works on last Tuesday's sample and quietly breaks on the real batch. Nobody can say afterward what it skipped.

Commissure is the middle path: the speed of automation with the paper trail of a careful analyst.

---

## Two examples

### Thousands of public comments, one analyst

When an agency proposes a rule, the public responds on regulations.gov. The responses are often long, technical, and written by people who know the subject well. Reading, categorizing, and summarizing them accurately used to take a team of subject matter experts. One federal policy office lost most of that team and kept the obligation.

A Commissure solution now downloads every comment on a docket, decodes the attachments, flags the ones that can't be read, categorizes and summarizes the rest, and produces the report. Local AI models do the reading, so no comment text leaves the machine. Before the report goes out, the analyst reviews a random sample of the results against the original comments, and the measured error rate goes into the report alongside the findings.

### Does this memo conflict with anything we've already said?

The same office checks new guidance for cross-cutting impacts. A memo about early childhood programs might touch mental health, Medicaid eligibility, or child care subsidies, and the office has to know whether it conflicts with, or is superseded by, guidance from three other agencies. That knowledge used to live in the heads of people who had read every memo for a decade.

The office loaded its archive of memos (a good share of them scans) into Commissure. When a new document arrives, Commissure finds the existing memos that overlap it and shows the passages side by side. An analyst makes the call on whether it's a conflict. The hours that went into finding are gone; the hours that go into deciding are the ones that were always worth spending.

---

## What Commissure does

- **Checks readability first.** Every PDF, Word file, and spreadsheet is inspected before anything else happens. Scanned, corrupt, or image-only pages are set aside with a stated reason, or sent through local OCR, never silently skipped.
- **Runs the same analysis on every document.** The questions, checks, and extractions are defined once and applied consistently across the whole set.
- **Keeps a pointer from every result to its source.** Each extracted fact records the page and passage it came from, in the exact version of the file that was ingested.
- **Makes the archive answerable.** Load a body of documents once and ask it questions as new material arrives: what overlaps this, what supersedes this, what have we already said.
- **Lets people verify the work.** A reviewer grades a random sample of results against the originals. The resulting error rate is published with the deliverable, so readers know how far to trust it.
- **Reruns only what changed.** When documents, parameters, or code change, only the affected work is redone, and the differences are reported.
- **Ships as a finished solution.** A completed solution is packaged so it can be handed to another team or client and run as-is.

---

## Why Commissure?

- **It lets fewer people do the work.** Not by lowering the standard, but by moving the hours from finding and sorting to reviewing and deciding.
- **Results you can defend.** Every claim links to a source. The review process produces a real accuracy figure, not a vibe.
- **Your data stays on your machine.** No cloud service, no uploads by default. Sensitive material (PHI, PII, pre-decisional policy) goes to a remote model only if a solution explicitly allows it. Local models are supported for when it can't.
- **AI where it helps, not everywhere.** Predictable work is done by predictable code. AI handles only the judgment calls, and every one of those calls is logged.
- **Built once, run again.** A solution is a reusable recipe plus one client's data. Monthly reruns and handoffs are the normal case, not a rebuild.
- **Cheap to own.** MIT licensed, plain Python, standard SQLite. No per-seat fees, no vendor to call, nothing exotic for your team to maintain.
- **Works with coding agents, under supervision.** Agents follow the same rules the framework enforces, and their output goes through the same review as anyone else's.

---

## What it isn't

- **Not a cloud service or a product you log into.** It runs where you run it. Unattended runs use the operating system's own scheduler.
- **Not an autonomous AI that works unsupervised.** It produces evidence for people to judge. It does not rule on whether a memo conflicts with policy; it shows you the passages and lets you decide.
- **Not a no-code tool.** Building a new solution takes a developer, or a coding agent working with one. Running an existing solution does not.

---

## For developers

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
