---
name: new-solution
description: >-
  Scaffold a new Commissure solution under solutions/<name>/ with `commissure init`,
  hand off a short operator checklist, and stop. Do not probe, investigate, or write
  stages. Use when asked to create, initialize, or scaffold a solution, recipe, or
  client operation.
---

# New solution

`commissure init` is the only scaffold. Never create `workspace/` or copy template files by hand.
Placeholder modules (`extract.py`, `analyze.py`, `deliver.py`) are not this step—leave them empty.

## Steps

1. **Name.** If `<name>` is not given, propose one kebab-case folder name and wait. Do not add a client prefix unless the operator uses one. Ledger stages are always `recipe.<stage>`.
2. **Init** from the repository root:
   ```bash
    uv run commissure init solutions/<name> --json
   ```

    `solutions/<name>/` may already exist. If any scaffold file is present the command exits 2 and writes nothing: stop and report; do not write by hand. The `--json` `created` list is the file check.
3. **Verify** against `solutions/<name>/` (the directory the operator will use). Exit 2 is the pass:
   ```bash
    uv run --directory solutions/<name> commissure run --step extract --json
   ```

    Pass means exit 2, stderr contains `STAGES`, and `workspace/` is absent. Do not fill `STAGES` to make it pass.
4. **Hand off** in a fixed short message, then **stop**. Do not start `investigate-solution` in the same turn.
   - Fill `docs/notes.md` (purpose, audience, deliverables, open questions).
   - Put a representative batch in `intake/<batch>/`, one subfolder per source to register. Loose files beside `intake/README.md` are the wrong layout. The full archive can wait.
   - Leave `recipe/`, `release.toml`, `CHANGELOG.md`, and `releases/` untouched. `release.toml` is filled at first release.
   - Intake documents and the workspace stay local (the scaffold `.gitignore`). Do not force-add them. A later secret is a gitignored `.env` plus `.env.example`, not part of this step.

## Do not

- Probe, investigate, write stages, schemas, or prompts.
- Add `solutions/<name>/tests` to `pyproject.toml`, create fixtures, or edit targets.
- Run git commands unless asked.
