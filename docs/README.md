# Commissure Documentation

[`AGENTS.md`](../AGENTS.md) at the repository root is the authoritative source of truth for Commissure's vocabulary, rules, and structural invariants. The documents in this folder provide permanent technical and operational reference:

- [`architecture.md`](architecture.md) — **Architecture & Core Concepts:** Workspaces (`state.db` + `blobs/`), source identity vs. parser drift, structural file reconnaissance (`probe`) and per-page readability gates, stage subjects (`item`, `group`, `run`), `RecordEnvelope` outcomes, automatic staleness fingerprinting, verified `Evidence` pointers, the `Verdict` log, and the three-tier repository layout.
- [`guide.md`](guide.md) — **Operator & Recipe Guide:** Solution folder layout, recipe authoring (`schema.py`, `extract.py`, `recipe.py`), CLI command reference (`init`, `probe`, `run`, `recon`, `inspect`, `search`, `review`, `serve`, `release`, `licenses`), the coding-agent skill workflow, client release packaging, and pinning Tesseract for OCR.
- [`contributing.md`](contributing.md) — **Development & Contributing:** Local development setup, test and fixture rules, the solution-vs.-core boundary (**Rule of Two**), third-party license policy (`commissure licenses`), and pull-request checklist.
- [`logo/README.md`](logo/README.md) — **Brand & Badge Assets:** Self-contained SVG marks, wordmarks, and status badges with dark-mode support, plus `build_logo.py`.
