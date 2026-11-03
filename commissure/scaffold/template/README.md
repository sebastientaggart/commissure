# __SOLUTION_NAME__ (`solutions/__SOLUTION_NAME__/`)

> Status: initialized __DATE__ by `commissure init`. No stages are defined yet; the recipe fails
> at startup on purpose (`STAGES` is empty) until an agent has investigated the inputs and
> filled in `recipe/AGENTS.md`.

Read the core playbook (`../../AGENTS.md`) and workflow skills (`../../.agents/skills/`) first.
`recipe/AGENTS.md` holds this operation's own rules and never overrides core invariants. Fill in
every **TODO** below before outputs ship.

## Setup & Agent Context

This repository is a Commissure solution designed to sit at `solutions/__SOLUTION_NAME__/` inside
a checkout of the open-source `commissure` substrate (Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)):

```sh
git clone https://github.com/<org>/commissure.git
cd commissure
git clone <this-repo-url> solutions/__SOLUTION_NAME__
uv sync
```

- **For coding agents:** Whether opened at the `commissure` root or inside `solutions/__SOLUTION_NAME__/`,
  read `../../AGENTS.md`, `../../commissure/api.py`, `../../.agents/skills/`, and `recipe/AGENTS.md`.
- **Solution vs. Core changes:** Keep domain schemas, extraction rules, and helpers inside
  `solutions/__SOLUTION_NAME__/` (**Rule of Two**) and verify `git status --short` in `../..` stays
  clean—local edits to `../../commissure/` will not ship with this repository. If a task uncovers
  a core bug or needs a domain-neutral substrate improvement, stop and propose a standalone,
  client-neutral pull request to `commissure` (tested in `../../tests/`).

## Purpose

**TODO:** what this solution does, for whom, and what it delivers (outputs and interface, if any).

## Layout

```text
solutions/__SOLUTION_NAME__/
  README.md       this file: the arrangements for this operation
  .gitignore      keeps workspace/, intake documents, and secrets out of git
  CHANGELOG.md    one entry per released version
  release.toml    version, allowlist of what ships, client-facing usage
  releases/       client targets (<target>.toml) and published tarballs (never shipped)
  docs/           reference documentation and operator notes (committed; indexed in docs/README.md)
  intake/         input documents (local; intake/README.md is tracked; never shipped)
  recipe/         code: recipe.py, schema.py, extract.py, analyze.py, deliver.py, AGENTS.md (what ships to a client)
  tests/          tests and fixtures (never shipped)
  workspace/      data: state.db, blobs/, outputs/ (created by commissure on the first run; local, never shipped)
```

## From empty folder to running recipe

1. **Operator populates.** Fill `docs/notes.md` (purpose, audience, deliverables, open questions).
   Drop a representative batch into `intake/<batch>/`, one folder per source that will be
   registered. Put further reference documentation and notes into `docs/`, adding one line per
   file to `docs/README.md`. If the recipe must also *process* a reference document as an input
   (role `reference`), it goes in `intake/` too. The full archive and any test fixtures can wait.
2. **Agent investigates** (skill: `investigate-solution`). Reads `docs/README.md` and every file
   it indexes, runs `commissure probe` over `intake/`, and fills in every section of
   `recipe/AGENTS.md` (purpose, inputs, domain rules, quarantine rules, acceptance gate,
   definition of done). No stage code is written until that file is complete and the operator
   has read it.
3. **Agent builds** (skill: `build-recipe-stage`). Declares `STAGES` in `recipe/recipe.py` one
   stage at a time: strict schema first, deterministic extraction second, `ctx.llm` only for
   residue; iterates with `--limit N --json`; proves every quarantine rule and gate can fail.

## Running (after stages exist)

Commands run from this directory. When invoked here with neither `--recipe` nor `--workspace`,
`commissure run` and `commissure inspect` default to `recipe/` and `workspace/` and record
`"solution-dir"` as the configuration source. `workspace/` is created on the first valid run.

```bash
cd solutions/__SOLUTION_NAME__

# 1. Reconnaissance before writing any extraction code (stateless, no workspace)
uv run commissure probe intake/<batch> --json

# 2. Register a source and ingest it (one source per cycle; roles are recipe-defined)
uv run commissure run --step ingest --source intake/<batch> --role corpus --json

# 3. Parse current items into text, structure, and the FTS5 index
uv run commissure run --step parse --json
#    Scanned pages, if probe reported any (needs the tesseract binary):
uv run commissure run --step parse --parser ocr --json

# 4. Iterate on a recipe stage over a small slice, then run it to completion
uv run commissure run --step <stage> --limit 10 --json
uv run commissure run --step <stage> --json

# 5. Read the workspace without locking or writing it
uv run commissure inspect --json
```

## Secrets

None yet. If a tool in this solution needs a credential, add a `.env.example` listing the
variable name, keep the real value in a gitignored `.env` read only by that tool, and never
share it with another solution (Configuration and hygiene 2).

## Tests

Put solution tests and fixtures in `tests/` (`uv run pytest` collects `solutions/*/tests` automatically).

## Definition of done

**TODO:** what the operator checks before outputs ship (mirrors `recipe/AGENTS.md`).
