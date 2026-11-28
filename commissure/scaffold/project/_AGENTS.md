# __SOLUTION_NAME__

A Commissure solution: a project that depends on the `commissure` package (exact version in `uv.lock`). Read `.agents/commissure/AGENTS.md` first: core's rules for recipe authors. They apply here without exception; this file and `recipe/AGENTS.md` add to them and never override them.

## Where things are

- `recipe/AGENTS.md`: this solution's purpose, inputs, domain and quarantine rules, and acceptance gate (filled in by the `investigate-solution` skill, approved by the operator).
- `docs/README.md`: index of this solution's reference documents; read every file it names.
- `.agents/commissure/`: core's rules, docs (`docs/guide.md`: recipe API and CLI; `docs/solutions.md`: lifecycle; `docs/architecture.md`), and skills, for the version in `.agents/commissure/VERSION`. It is core's: never edit it. `uv run commissure agent-kit` replaces it after a core upgrade.

## Rules

- **This solution only.** Never edit commissure's installed code under `.venv/`. When a task needs a core change, stop and draft an upstream request (`.agents/commissure/AGENTS.md`, "Core changes").
- **Git:** commit or push only when asked.

## Commands

```sh
uv run commissure probe intake/<batch> --json
uv run commissure run --step <stage> --limit 5 --json
uv run commissure inspect --json
uv run commissure serve                  # interface (if any) and /review
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
