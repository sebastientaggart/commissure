<p align="center">
  <img src="docs/logo/logo-stacked.svg" alt="Commissure" width="280">
</p>

<p align="center">
  <a href="pyproject.toml"><img src="docs/logo/badge-python.svg" alt="python: 3.12+"></a>
  <a href="commissure/kit/docs/architecture.md"><img src="docs/logo/badge-pydantic.svg" alt="pydantic: v2"></a>
  <a href="commissure/kit/docs/architecture.md"><img src="docs/logo/badge-storage.svg" alt="storage: SQLite"></a>
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
- **One solution per client:** Each solution is its own small project and repository; a client receives only theirs, with Commissure pinned in its lock file.

## Why it suits AI agents

Versioned rules ([`commissure/kit/AGENTS.md`](commissure/kit/AGENTS.md)) and workflow skills ([`commissure/kit/skills/`](commissure/kit/skills/)), copied into every solution, keep coding agents within checkable limits (strict schemas, small slices, and proven-to-fail tests), so their work stays verifiable.

## When not to use it

- **Not a hosted cloud service:** Runs where you install it; unattended runs use the OS scheduler.
- **Not unsupervised AI:** Produces cited evidence for people to judge rather than making policy decisions on its own.
- **Not a no-code builder:** Building a new solution requires a developer with an agent; running an existing solution does not.

---

## Getting Started

Requires [`uv`](https://docs.astral.sh/uv/) (`brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`). Nothing else: no clone, no model, no service.

**Try the example (5 minutes).** A working solution over 23 fictional public comments: positions and topics, each pointing at the words it rests on, with a scan set aside and a review page.

```sh
uvx commissure new demo --example && cd demo && uv sync
uv run commissure run --step ingest --source intake/comments --role comment
uv run commissure run --step parse
uv run commissure run --step extract
uv run commissure run --step tally
uv run commissure serve      # the results, and /review to grade a random sample
```

**Start your own (5 more minutes).**

```sh
uvx commissure new my-solution && cd my-solution && git init && uv sync
```

Put documents in `intake/<batch>/`, describe the job in `docs/notes.md`, and open your coding agent in the folder: `AGENTS.md`, core's rules, and the `investigate-solution` and `build-recipe-stage` skills are already there. The lifecycle, step by step: [`solutions.md`](commissure/kit/docs/solutions.md).

## Documentation & License

- **[Solution Developer Guide](commissure/kit/docs/solutions.md):** The lifecycle from the example to a release, operator vs. agent responsibilities, `recipe/tools/` fetchers, `intake/` curation, and roles.
- **[Operator & Recipe Reference](commissure/kit/docs/guide.md):** Solution layout, recipe authoring, CLI reference, graph entities/edges, review commands, releasing, and OCR setup.
- **[Architecture & Core Concepts](commissure/kit/docs/architecture.md):** Workspaces, source vs. parser drift, readability gates, stage staleness, `Evidence`, and `Review`.
- **[Rules for Recipe Authors](commissure/kit/AGENTS.md):** Vocabulary and the invariants every recipe follows.
- **[Contributing to Core](docs/contributing.md):** Setup, working with coding agents on `commissure/`, verification order, and how changes reach `dev`.

Licensed under the [MIT License](LICENSE).
