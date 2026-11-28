# Contributing to Commissure

Core changes happen in `commissure/` and `tests/`. [`AGENTS.md`](../AGENTS.md) holds the rules for core work, and [`commissure/kit/AGENTS.md`](../commissure/kit/AGENTS.md) the vocabulary and invariants core shares with every recipe.

## Setup

```sh
git clone https://github.com/sebastientaggart/commissure && cd commissure
uv sync --all-extras
uv run pytest
brew install tesseract && brew pin tesseract   # optional: needed for tests/test_ocr.py
```

- **Try a core change against a solution** without touching its lock, from the solution's folder: `uv run --with-editable ../commissure pytest` (or `... commissure run ...`). The next plain `uv run` is back on the locked version.
- **Change the templates or the kit** (`commissure/scaffold/`, `commissure/kit/`): `COMMISSURE_SCAFFOLD_INTEGRATION=1 uv run pytest tests/test_scaffold.py` builds the wheel, scaffolds both templates from it, and runs their `uv sync`, tests, and ruff. The example's corpus is generated: `uv run python tests/fixtures/make_example_corpus.py`.

## Work with your agent like this

1. **Point the agent at the right rules first:**
   - **Core substrate (`commissure/`):** [`AGENTS.md`](../AGENTS.md), [`commissure/kit/AGENTS.md`](../commissure/kit/AGENTS.md), and [`architecture.md`](../commissure/kit/docs/architecture.md).
   - **Dependencies or vendored browser assets:** [`AGENTS.md` Section 2](../AGENTS.md) and [`commissure/tools/licenses.toml`](../commissure/tools/licenses.toml).
   - **Core bug uncovered during solution work:** Stop the solution task ([`build-recipe-stage` Boundaries](../commissure/kit/skills/build-recipe-stage/SKILL.md)) and handle the core fix here, with a synthetic test.
2. **Follow this order on every change:**
   1. **Plan:** Agree on the minimal change in `commissure/` (and [`commissure/api.py`](../commissure/api.py) if recipes need it). Stop if it conflicts with [`AGENTS.md`](../AGENTS.md).
   2. **Proven-to-fail test:** Write a synthetic test in `tests/` ([`AGENTS.md` Section 3](../AGENTS.md)), run `uv run pytest <test>` before editing `commissure/`, and confirm it fails for the expected reason ([`commissure/kit/AGENTS.md` Section 4](../commissure/kit/AGENTS.md)).
   3. **Change:** Implement the fix and leave a short comment at any non-obvious threshold, lock, or refusal guarded by the test.
   4. **Equivalence check:** Before replacing an existing parser, query, or code path, prove matching or intentionally improved outputs on fixtures and parse goldens ([`commissure/kit/AGENTS.md` Section 4](../commissure/kit/AGENTS.md) and [`AGENTS.md` Section 3](../AGENTS.md)).
   5. **Verify:** Run the suite and report commands and results verbatim:
      ```sh
      uv run pytest
      uv run ruff check .
      uv run ruff format --check .
      uv run commissure licenses
      ```

## Boundaries

- **Core vs. solution boundary:** `commissure/` and `tests/` never contain a client, office, solution name, corpus name, or machine path ([`AGENTS.md` Section 1](../AGENTS.md); `tests/test_repository.py` checks paths and credentials).
- **Rule of Two:** Keep new helpers inside a solution's `recipe/` on first use and graduate them to `commissure/patterns/` only when a second recipe needs them ([`commissure/kit/AGENTS.md` Section 3](../commissure/kit/AGENTS.md)).

## How a change reaches `dev`

Day-to-day work lands on `dev`; `main` receives only release pull requests from `dev` ([`AGENTS.md` Section 2](../AGENTS.md)). Commits must be signed. Confirm the verification commands above exit `0`, then commit and open a PR to `dev` (the operator runs `git` commands unless explicitly delegated). A change a solution must act on adds an **Upgrading** entry to `CHANGELOG.md`, and a change to the recipe API or a shared rule updates `commissure/kit/` in the same commit.
