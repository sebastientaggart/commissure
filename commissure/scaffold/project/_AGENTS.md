# __SOLUTION_NAME__

A Commissure solution: a project that depends on the `commissure` package (exact version in `uv.lock`). Read `.agents/commissure/AGENTS.md` first: core's rules for recipe authors apply here without exception, and this file adds to them and never overrides them.

This file is the solution's contract. The `investigate-solution` skill fills in the sections below, the operator approves them, and `build-recipe-stage` builds from them. Until then `recipe/recipe.py` declares no stages and fails at startup on purpose. Keep it to the contract: measurements, recon output, history, and plans go in `docs/` (indexed in `docs/README.md`).

## Working here

- `.agents/commissure/`: core's rules, docs (`docs/guide.md`: recipe API and CLI; `docs/solutions.md`: lifecycle; `docs/architecture.md`), and skills, for the version in `.agents/commissure/VERSION`. It is core's: never edit it. `uv run commissure agent-kit` replaces it after a core upgrade.
- `docs/README.md`: index of this solution's reference documents; read every file it names.
- **This solution only.** Never edit commissure's installed code under `.venv/`. When a task needs a core change, stop and draft an upstream request (`.agents/commissure/AGENTS.md`, Boundaries).
- **Git:** commit or push only when asked.

```sh
uv run commissure probe intake/<batch> --json
uv run commissure run --step <stage> --limit 5 --json
uv run commissure inspect --json
uv run commissure serve                  # interface (if any) and /review
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Purpose

What operation this recipe performs, for whom, and what outputs (and interface, if any) it produces. Note any optional extras this recipe requires (for example `commissure[docx]` for `docxtpl` template rendering).

## Inputs

Sources, locators, roles (`corpus`, `reference`, ...), file types, and what reconnaissance (`commissure probe`) found: scanned pages, glyph anomalies, schema edge cases.

## Domain rules

Client-specific rules and edge cases, each with the input that motivated it.

## Quarantine rules

Every condition that yields `warning` or `quarantined`, with its reason code and the test that proves it fires.

## Acceptance gate

Thresholds on `discovered`, `attempted`, `valid`, `warning`, `quarantined`, `skipped` that make a run pass or fail, and any core fail-closed default this recipe tightens or relaxes.

## Definition of done

What the operator checks before outputs ship.
