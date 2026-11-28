# __SOLUTION_NAME__

> **TODO:** what this solution does, for whom, and what it delivers. Created __DATE__ by `commissure new`; no stages are defined yet.

A [Commissure](https://github.com/sebastientaggart/commissure) solution. Everything runs on this machine; input documents in `intake/` and results in `workspace/` never leave it and are never committed.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/):

```sh
uv sync
```

## Run

From this folder (`commissure` defaults to `recipe/` and `workspace/` here):

```sh
# Register a folder of documents and ingest it (one source per batch)
uv run commissure run --step ingest --source intake/<batch> --role corpus --json

# Extract text; scanned pages need --parser ocr and the tesseract binary
uv run commissure run --step parse --json

# TODO: the recipe's stages, in order
uv run commissure run --step <stage> --json

# Results, and /review to sample and grade them against the originals
uv run commissure serve
```

## Layout

```text
AGENTS.md, CLAUDE.md   rules for coding agents (core's are in .agents/commissure/)
recipe/                the code: stages, schemas, interface; recipe/AGENTS.md is its contract
tests/                 tests and small synthetic fixtures (uv run pytest)
docs/                  reference documents and operator notes, indexed in docs/README.md
intake/                input documents (local; only intake/README.md is tracked)
workspace/             state.db, blobs/, outputs/ (created by the first run; local)
CHANGELOG.md           one entry per released version
```

## Releasing

Bump `version` in `pyproject.toml`, add its entry to `CHANGELOG.md`, run `uv run pytest`, commit, and tag `v<version>`. The repository at that tag is the release.
