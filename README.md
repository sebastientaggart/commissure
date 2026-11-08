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

**Commissure** is an open-source tool that turns large folders of PDFs, Word files, spreadsheets, and records into checked, cited findings and local review apps.

## The problem

Teams have to read, cross-check, and summarize thousands of public comments, policy memos, or client documents without missing unreadable scans or losing track of where a claim came from. Spreadsheets are too slow, chat tools guess and cannot cite exact passages at scale, and one-off scripts fail silently on bad files.

## What makes it different

- **Machine-checked citations:** Every result points at the exact page and passage in the original file. Commissure verifies every span against the extracted text before saving it and sets unreadable pages aside for local OCR or human review.
- **Human sampling and error rates:** Reviewers grade a seeded random sample of results against the originals side by side, producing a statistical error rate published with the findings.
- **Offline on local hardware:** Runs on one machine in SQLite. Documents and sensitive data stay local unless a stage explicitly enables a remote model.
- **One solution per client:** Each client receives a self-contained package with only their recipe, user guide, and a pinned build of Commissure.

## Why it suits AI agents

Versioned rules ([`AGENTS.md`](AGENTS.md)) and workflow skills ([`.agents/skills/`](.agents/skills/)) keep coding agents within checkable limits (strict schemas, small slices, and proven-to-fail tests), so their work stays verifiable.

## When not to use it

- **Not a hosted cloud service:** Runs where you install it; unattended runs use the OS scheduler.
- **Not unsupervised AI:** Produces cited evidence for people to judge rather than making policy decisions on its own.
- **Not a no-code builder:** Building a new solution requires a developer with an agent; running an existing solution does not.

---

## Getting Started

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/):

```sh
brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                  # creates .venv and installs dependencies
uv run commissure --help
```

- **Build or extend a solution:** See [`docs/solutions.md`](docs/solutions.md).
- **Run an existing solution:** Open `solutions/<name>/docs/user-guide.md` (or `solutions/<name>/README.md`).
- **Contribute to core:** See [`docs/contributing.md`](docs/contributing.md).

## Documentation & License

- **[Solution Developer Guide](docs/solutions.md):** Lifecycle mapped to skills, operator vs. agent responsibilities, `recipe/tools/` fetchers, `intake/` curation, and roles.
- **[Operator & Recipe Reference](docs/guide.md):** Solution layout, recipe authoring, CLI reference, graph entities/edges, review commands, and OCR setup.
- **[Architecture & Core Concepts](docs/architecture.md):** Workspaces, source vs. parser drift, readability gates, stage staleness, `Evidence`, and `Review`.
- **[Contributing to Core](docs/contributing.md):** Working with coding agents on `commissure/`, verification order, boundaries, and how changes reach `dev`.
- **[`AGENTS.md`](AGENTS.md):** Authoritative rules, vocabulary, and invariants.

Licensed under the [MIT License](LICENSE).
