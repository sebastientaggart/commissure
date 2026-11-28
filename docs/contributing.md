# Contributing to Commissure

Core changes happen in `commissure/` and `tests/`. [`AGENTS.md`](../AGENTS.md) is the single source of truth for vocabulary, invariants, and license rules.

## Setup

```sh
uv sync --all-extras
brew install tesseract && brew pin tesseract   # optional: needed for tests/test_ocr.py
```

## Work with your agent like this

1. **Point the agent at the right rules first:**
   - **Core substrate (`commissure/`):** [`AGENTS.md` Sections 1 & 2](../AGENTS.md) and [`docs/architecture.md`](architecture.md).
   - **Dependencies or vendored browser assets:** [`AGENTS.md` Section 3.8](../AGENTS.md) and [`commissure/tools/licenses.toml`](../commissure/tools/licenses.toml).
   - **Core bug uncovered during solution work:** Stop the solution task ([`build-recipe-stage` Boundaries](../.agents/skills/build-recipe-stage/SKILL.md)) and handle the core fix in a standalone branch.
2. **Follow this order on every change:**
   1. **Plan:** Agree on the minimal change in `commissure/` (and [`commissure/api.py`](../commissure/api.py) if recipes need it). Stop if it conflicts with [`AGENTS.md`](../AGENTS.md).
   2. **Proven-to-fail test:** Write a synthetic test in `tests/` ([`AGENTS.md` Section 5](../AGENTS.md)), run `uv run pytest <test>` before editing `commissure/`, and confirm it fails for the expected reason ([`AGENTS.md` Section 2.1](../AGENTS.md)).
   3. **Change:** Implement the fix and leave a short comment at any non-obvious threshold, lock, or refusal guarded by the test.
   4. **Equivalence check:** Before replacing an existing parser, query, or code path, prove matching or intentionally improved outputs on fixtures and parse goldens ([`AGENTS.md` Sections 2.3 & 5](../AGENTS.md)).
   5. **Verify:** Run the suite and report commands and results verbatim:
      ```sh
      uv run pytest
      uv run ruff check .
      uv run ruff format --check .
      uv run commissure licenses
      ```

## Boundaries

- **Core vs. solution boundary:** `commissure/` and `tests/` never contain a client, office, solution name, corpus name, or machine path ([`AGENTS.md` Section 1](../AGENTS.md), enforced by `scan_forbidden` in [`commissure/tools/release.py`](../commissure/tools/release.py)).
- **Rule of Two:** Keep new helpers inside `solutions/<name>/recipe/` on first use and graduate them to `commissure/patterns/` only when a second recipe needs them ([`AGENTS.md` Section 1](../AGENTS.md)).

## How a change reaches `dev`

Day-to-day work lands on `dev`; `main` receives only release pull requests from `dev` ([`AGENTS.md` Section 3.6](../AGENTS.md)). Confirm `git status --short` has no untracked solution files and the verification commands above exit `0`, then commit and open a PR to `dev` (the operator runs `git` commands unless explicitly delegated).
