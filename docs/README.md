# Commissure Documentation

[`AGENTS.md`](../AGENTS.md) holds the rules for working on core. The rules and docs every solution follows ship inside the package, in [`commissure/kit/`](../commissure/kit/), and `commissure new` copies them into each solution's `.agents/commissure/`:

- [`commissure/kit/AGENTS.md`](../commissure/kit/AGENTS.md): **Rules for Recipe Authors:** Vocabulary, evidence and verdicts, boundaries and upstream requests, verification discipline, data sovereignty, the lifecycle and skills, and tests.
- [`commissure/kit/docs/solutions.md`](../commissure/kit/docs/solutions.md): **Solution Developer Guide:** The lifecycle from the example to a release (`commissure new`, `investigate-solution`, `build-recipe-stage`, upgrading core, releasing), operator vs. agent responsibilities, `recipe/tools/` fetchers, the collect/curate/ingest pattern for `intake/`, and roles.
- [`commissure/kit/docs/guide.md`](../commissure/kit/docs/guide.md): **Operator & Recipe Reference:** Solution layout, recipe authoring (`schema.py`, `extract.py`, `recipe.py`), CLI command reference (`new`, `agent-kit`, `probe`, `run`, `recon`, `inspect`, `search`, `review`, `serve`, `licenses`), keeping verdicts, releasing a solution, and pinning Tesseract for OCR.
- [`commissure/kit/docs/architecture.md`](../commissure/kit/docs/architecture.md): **Architecture & Core Concepts:** Workspaces (`state.db` + `blobs/`), source identity vs. parser drift, structural file reconnaissance (`probe`) and per-page readability gates, stage subjects (`item`, `group`, `run`), `RecordEnvelope` outcomes, automatic staleness fingerprinting, verified `Evidence` pointers, the `Verdict` log, and the package layout.

In this folder:

- [`contributing.md`](contributing.md): **Contributing to Core:** Setup, working with coding agents on `commissure/`, the verification order (plan, proven-to-fail test, change, equivalence check, pytest/ruff), trying a core change against a solution, and how changes reach `dev`.
- [`logo/README.md`](logo/README.md): **Brand & Badge Assets:** Self-contained SVG marks, wordmarks, and status badges with dark-mode support, plus `build_logo.py`.
