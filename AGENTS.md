# Commissure Agent Playbook (`AGENTS.md`)

This file holds the rules for working on Commissure core. **Read [`commissure/kit/AGENTS.md`](commissure/kit/AGENTS.md) first:** the vocabulary and the invariants that core and every recipe share (evidence, verdicts, boundaries, verification, data sovereignty). It ships inside the package and every solution carries a copy, so a shared rule changes there, never here. Every invariant in either file applies without exception.

**When a task appears to require violating a principle, stop and say so.** Report the conflict, the principle, and the options. Never quietly deviate, work around a rule, or reinterpret it to fit the task.

`obsidian-notes/` at the repository root, when present, is a symlink to the operator's planning notes for this project (ignored by git and skipped by repo-wide search); read it for intent and status, and keep anything private there, never in tracked files.

---

## 1. Repository Tiers

- **`commissure/` (Core Substrate — Keep Small):**
  - Owns storage (`state.db` in SQLite `WAL` mode + sharded content-addressed `blobs/` + `outputs/`; `commissure prune` clears superseded unreferenced `extracted_text` bodies while keeping rows, `text_hash`, and outcomes), `(source, locator)` → `item_version` versioning (`bytes_hash` for byte identity, `text_hash` per `(item_version, parser, parser_version)` for content drift, tombstones on removal, `latest_seen` kept separate from `latest_valid`), generic `entities` and `edges` tables (`commissure/graph.py`: shape, provenance, and per-outcome snapshots in core; meaning in recipes), per-page readability gates and parsers (run in a bounded local process pool; opt-in `ocr` parser over local Tesseract emitting `warning` text with per-page confidence), `RecordEnvelope` and outcome ledger, bounded thread-pool runner with per-item SQLite checkpointing and `_CONN_LOCK` (main thread commits outcomes; worker threads touch SQLite only through `ctx.llm`), FTS5 search, the single `ctx.llm` seam (`commissure/llm.py`: `extract` and `complete`, content-addressed `llm_cache` cached before domain validation, `llm_calls` ledger, tolerant coercion boundary, transient retries, and systematic-failure circuit breaker), CLI inspection (`commissure/inspect.py`, `commissure/recon.py`), the verdict log (`commissure/review.py`, `commissure review`), offline HTML shell (`commissure/deliver/`), and single-process HTTP mount (`commissure/serve.py`), which mounts only a recipe's `INTERFACE`: core adds no web routes without one. Beside an interface, core owns the review page (`commissure/review_page.py`, `/review/*`): the verdict log's interface over the same functions as `commissure review`, with originals served from blobs under `Content-Security-Policy: sandbox`, only inert types (PDF, raster images, plain text, and JSON as a script-free collapsible tree) shown inline, and every other type a download.
  - Evidence is checked and resolved in `commissure/evidence.py` (the `evidence` table, the `evidence_review` view); the verdict log is `commissure/review.py`. Core documents the safe copy of verdicts and adds no export, import, or backup command.
  - **Surface:** `commissure/api.py` is the recipe surface. CLI commands and exit codes live in `commissure/cli.py`.
  - **Hard rules:** Core never references a client, corpus, solution, or business document type, and no tracked file names a machine path. Core runtime dependencies and vendored assets are permissive (Section 2).
- **`commissure/patterns/` (Graduated Reusable Helpers):** helpers a second recipe needed (Rule of Two).
- **`commissure/scaffold/` and `commissure/kit/` (What a Solution Starts From):** `commissure new` copies `scaffold/project/` (or `scaffold/example/`, a working recipe over a small synthetic corpus) and the agent kit: `kit/AGENTS.md`, `kit/docs/`, and `kit/skills/` (linked in this repository's `.claude/skills/`). Both ship in the wheel. The example is fictional and stays so.
- **`commissure/tools/` (Repository Tooling):** `licenses.py` with its register `licenses.toml`, mounted as `commissure licenses`. Never imported by recipes or runtime core modules, never touches `state.db`.
- **Solutions are not here.** Each solution is its own project depending on a released `commissure` (`kit/docs/solutions.md`). Contributors upstream core changes via pull requests to `dev` that contain zero client, office, solution, or corpus names and include proven-to-fail synthetic tests in `tests/`. A core change a solution must act on lands with an **Upgrading** entry in `CHANGELOG.md`.

---

## 2. Core-Only Rules

1. **Git hygiene:** Day-to-day work happens directly on `dev`; `main` only receives release PRs from `dev`. Commits must be signed (both branches are protected).
2. **Third-party licenses:** Core runtime dependencies and vendored browser assets use only the licenses allowed in `commissure/tools/licenses.toml` (MIT, BSD, Apache-2.0, ISC, and kin). Weak copyleft (LGPL, MPL) enters only as an optional extra that `commissure/` never imports (`commissure[docx]`), with a recorded exception. Strong copyleft (GPL, AGPL), source-available (SSPL, BUSL), and unlicensed code never enter. Before adding or upgrading a dependency or vendored file, check its license, including native libraries bundled in binary wheels (`*.dist-info/licenses/`), which package metadata does not describe; record anything outside the allowlist as an exception, and only on the operator's decision. `tests/test_licenses.py` enforces the register. Answer license questions ("what non-MIT licenses do you use?") by running `commissure licenses` (`--json`) and citing its exceptions, never from memory or raw metadata.
3. **The kit is versioned with core:** A change to `commissure/api.py`, the CLI, or a rule recipes follow updates `commissure/kit/` in the same commit.

---

## 3. Tests and Fixtures

- Environment: `uv sync --all-extras`
- Tests: `uv run pytest` (single file/test: `uv run pytest tests/test_ingest.py::test_name`)
  - Tests run in parallel by default (`-n auto` in `pyproject.toml`); add `-n0` to see `print` output or use `pdb`.
- Lint & format: `uv run ruff check .` and `uv run ruff format --check .` (apply with `uv run ruff format .`). The scaffold templates are linted as the projects they become, by `tests/test_scaffold.py`.

Rules:
1. Core tests live in `tests/` (`ws` / `ws_root` and `cli` fixtures in `tests/conftest.py`). Input fixtures in `tests/fixtures/` are copied into `tmp_path` before use and never modified in place; generated fixtures (`tests/fixtures/make_probe_corpus.py`) are never hand-edited.
2. Real samples go in `tests/fixtures/real_samples/` with a human-verified entry in `tests/fixtures/real_samples.expected.json` (`tests/test_real_samples.py`).
3. Parse goldens live in `tests/golden/parse/`; regenerate with `COMMISSURE_UPDATE_GOLDEN=1 uv run pytest tests/test_parse_golden.py` and review `git diff tests/golden` line by line.
4. Runner behaviour is tested with `tests/recipes/demo/`, picking subjects by locator rather than commit order. The example solution is tested end to end in `tests/test_example.py`.

---

## 4. Releasing Core

Bump `version` in `pyproject.toml`, move the `CHANGELOG.md` **Unreleased** entries under the new version (with an **Upgrading** entry for anything a recipe must change), merge the release PR from `dev` to `main`, and tag `v<version>` on `main`. The tag's workflow (`.github/workflows/release.yml`) tests, builds, and publishes to PyPI. Versions are `0.x`: a minor release may break recipes, a patch release never does (solutions depend on `commissure~=X.Y.Z`).
