# Commissure Documentation

[`AGENTS.md`](../AGENTS.md) at the repository root is the authoritative source of truth for Commissure's vocabulary, rules, and structural invariants. The documents in this folder provide permanent technical and operational reference:

- [`solutions.md`](solutions.md): **Solution Developer Guide:** The solution lifecycle mapped to agent skills (`new-solution`, `investigate-solution`, `build-recipe-stage`, `release-target`), operator vs. agent responsibilities, `recipe/tools/` fetchers, the collect/curate/ingest pattern for `intake/`, and roles.
- [`guide.md`](guide.md): **Operator & Recipe Reference:** Solution folder layout, recipe authoring (`schema.py`, `extract.py`, `recipe.py`), CLI command reference (`init`, `probe`, `run`, `recon`, `inspect`, `search`, `review`, `serve`, `release`, `licenses`), client release packaging, and pinning Tesseract for OCR.
- [`architecture.md`](architecture.md): **Architecture & Core Concepts:** Workspaces (`state.db` + `blobs/`), source identity vs. parser drift, structural file reconnaissance (`probe`) and per-page readability gates, stage subjects (`item`, `group`, `run`), `RecordEnvelope` outcomes, automatic staleness fingerprinting, verified `Evidence` pointers, the `Verdict` log, and the three-tier repository layout.
- [`contributing.md`](contributing.md): **Contributing to Core:** Working with coding agents on `commissure/`, the verification order (plan, proven-to-fail test, change, equivalence check, pytest/ruff), core vs. solution boundary, **Rule of Two**, and how changes reach `dev`.
- [`logo/README.md`](logo/README.md): **Brand & Badge Assets:** Self-contained SVG marks, wordmarks, and status badges with dark-mode support, plus `build_logo.py`.
