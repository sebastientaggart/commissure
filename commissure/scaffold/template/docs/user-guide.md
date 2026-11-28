# __SOLUTION_NAME__: user guide

For the analysts and subject-matter experts (SMEs) who run this solution. Commands run from `solutions/__SOLUTION_NAME__/`.

1. **Collect** documents into `intake/<source>/`.
2. **Check** what was collected against the manifests.
3. **Curate**: an SME decides what belongs in the corpus and in which role.
4. **Process**: ingest, parse, run the stages, and review the results.
5. **Back up** the review verdicts.

## 1. Collect

**TODO:** how each source folder under `intake/` is populated (for example, `uv run python recipe/tools/fetch_<name>.py` or manual download steps):

| Folder | What | From |
| --- | --- | --- |
| `intake/<source>/` | **TODO:** document set | **TODO:** origin or API |

## 2. Check

**TODO:** if a fetcher writes `intake/manifests/<source>.json`, list the statuses an SME should check (`saved`, `unchanged`, `changed_upstream`, `missing_pdf`, `http_404`) and what action each requires.

## 3. Curate

Check each source folder before the first ingest and after each refresh:

- **Relevance:** **TODO:** what belongs in this analysis and what is out of scope.
- **Keep superseded documents:** **TODO:** whether historical or superseded items must stay for lineage checks.
- **Roles:** Keep each file in the folder for its role (Process, below).
- **Legibility:** Scanned PDFs with no text layer need `--parser ocr` (Process).

To exclude a file without losing the decision, move it and record why in `intake/_excluded/CURATION.md`:

```bash
mkdir -p intake/_excluded/<source>
git mv intake/<source>/<file> intake/_excluded/<source>/
```

```text
YYYY-MM-DD  <source>/<file>  excluded: <reason>  (initials)
```

Ingest only individual `intake/<source>` folders, never `intake/` as a whole.

## 4. Process

```bash
# TODO: one ingest command per source folder and role:
uv run commissure run --step ingest --source intake/<source> --role <role> --json

uv run commissure run --step parse --json
uv run commissure run --step parse --parser ocr --json   # if scanned PDFs exist; needs tesseract

# TODO: recipe stages in order:
uv run commissure run --step <stage> --json
```

Roles:

| Role | For | Treated as |
| --- | --- | --- |
| **TODO:** `<role>` | **TODO:** which documents | **TODO:** how stages use them |

Results:

- **TODO:** `workspace/outputs/recipe.<stage>/<run>/<file>`: what it contains.
- `uv run commissure serve`: local web interface (if defined) and `/review` for sampling and grading results.

## 5. Back up

Review verdicts are the only part of `workspace/` that cannot be rebuilt: they live in `workspace/state.db`. Back it up with SQLite's own copy, which is safe while a run or review is in progress:

```bash
sqlite3 workspace/state.db ".backup '/path/to/backups/state-$(date +%F).db'"
```

Never copy a live `state.db` with `cp` or a sync tool; point those at the backup file instead. To restore, stop Commissure, move the old `state.db`, `state.db-wal`, and `state.db-shm` aside, and copy the backup in as `workspace/state.db`.
