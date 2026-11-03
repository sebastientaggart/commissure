# Contributing to Commissure

Thank you for your interest in contributing to Commissure! Before making changes, read [`AGENTS.md`](../AGENTS.md) at the repository root—it is the single source of truth for vocabulary, architectural boundaries, and verification rules.

---

## 1. Local Development Setup

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/):

```sh
brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-extras     # creates .venv and installs dev + optional extras
brew install tesseract   # optional: required only to run OCR tests (tests/test_ocr.py)
```

Run the verification suite before submitting any change:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run commissure licenses
```

---

## 2. Solution vs. Core Boundary (The Rule of Two)

- **Keep operation-specific code in** `solutions/<name>/recipe/`**:** Domain schemas, extraction rules, prompts, deliverables, and first-use helpers belong inside the solution's recipe.
- **Graduate helpers on second use (**`commissure/patterns/`**):** Move a helper into `commissure/patterns/` only when at least two recipes need it and its interface is completely domain- and client-neutral.
- **Keep** `commissure/` **small and neutral:** Nothing in core code, tests, fixtures, comments, or commit messages may reference a client, office, solution slug, internal URL, or business document type.

---

## 3. Engineering & Verification Discipline

Every core contribution must follow the verification rules in `AGENTS.md` [Section 2](../AGENTS.md):

1. **Prove every check can fail:** Before trusting a new test, validation rule, or gate, run it with the fix reverted or against a malformed input and confirm it fails for the expected reason.
2. **Test the production schema path:** Initialize and migrate SQLite workspaces in tests exclusively through `open_workspace` (using the `ws`, `ws_root`, and `cli` fixtures in [`tests/conftest.py`](../tests/conftest.py))—never hand-rolled SQL tables.
3. **Never mutate fixtures in place:** Copy input fixtures from `tests/fixtures/` into `tmp_path` before modifying them. Never hand-edit generated files in `tests/fixtures/probe_corpus/` or `tests/fixtures/ocr_corpus/`; edit their generator scripts (`make_probe_corpus.py`, `make_ocr_corpus.py`) and rerun them.
4. **No client work product in fixtures:** Keep test fixtures in `tests/fixtures/` small, synthetic, and free of client work product. When parser behavior changes intentionally, regenerate parse goldens with `COMMISSURE_UPDATE_GOLDEN=1 uv run pytest tests/test_parse_golden.py` and review `git diff tests/golden` line by line.
5. **Verify output artifacts structurally:** Tests for HTML, DOCX, XLSX, CSV, or PDF outputs must assert structural validity (non-zero size, expected sections/rows/tables, zero external CDN URLs).

---

## 4. Third-Party License Policy

Core runtime dependencies and vendored browser assets in `commissure/deliver/static/` must use permissive licenses listed in [`commissure/tools/licenses.toml`](../commissure/tools/licenses.toml) (`MIT`, `BSD-2-Clause`, `BSD-3-Clause`, `Apache-2.0`, `ISC`, `0BSD`, `PSF-2.0`).

- Weak copyleft (`LGPL`, `MPL`) is permitted only as an optional extra (`commissure[docx]`) that `commissure/` never imports, with a documented exception in `licenses.toml`.
- Strong copyleft (`GPL`, `AGPL`), source-available (`SSPL`, `BUSL`), and unlicensed dependencies are never permitted.
- [`tests/test_licenses.py`](../tests/test_licenses.py) and `uv run commissure licenses` enforce the register on every test run.

---

## 5. Pull Request Checklist

- [ ] Zero client names, office names, solution slugs, machine paths (`/Users/...`, `/home/...`), or email addresses in code, tests, fixtures, or commit messages (verified against `DEFAULT_FORBID` in [`commissure/tools/release.py`](../commissure/tools/release.py)).
- [ ] New or updated tests in `tests/` proven to fail without the change.
- [ ] `uv run pytest tests`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run commissure licenses` all pass with exit code `0`.
