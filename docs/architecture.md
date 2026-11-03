# Architecture & Core Concepts

Commissure separates the machinery that stays constant across document and data operations (`commissure/`) from the schemas, domain rules, and deliverables specific to one engagement (`solutions/<name>/recipe/`).

For the normative rules and vocabulary that govern both tiers, see [`AGENTS.md`](../AGENTS.md).

---

## 1. Workspaces & Storage

Each workspace is a local directory holding one operational cycle (for example, one review, one docket, or one month's submissions):

- **`state.db`:** A single SQLite database in `WAL` mode. One shared connection is serialized by `_CONN_LOCK`; the main thread commits stage outcomes while a bounded local worker pool executes stage functions (touching SQLite from workers only through the thread-safe `ctx.llm` cache/ledger seam).
- **`blobs/`:** Sharded, content-addressed storage (`blobs/<sha256[:2]>/<sha256>`) holding the exact ingested bytes of every source item. Source input folders are strictly read-only; parsers and stages read from `blobs/`.
- **`outputs/`:** Rendered deliverables written by recipe stages and described by validated records.
- **`workspace.lock`:** A non-blocking advisory lockfile held by writers (`commissure run`). Read-only inspection commands (`inspect`, `recon`, `search`) and human review (`review`, `/review`) do not take the workspace lock and can run concurrently while a long stage executes.

When an operational cycle finishes, its workspace remains a self-contained archive that later code changes never mutate.

---

## 2. Source Identity vs. Tool Drift

The unit of input in core is an **Item**—a PDF, DOCX, XLSX workbook, CSV, JSON document, or structured row—tracked across time without conflating upstream source edits with local parser changes:

- **`(source, locator)` → `item_version`:** Each ingested file or object is mapped to append-only `item_version` rows recording `bytes_hash`, `size_bytes`, `role`, `run_id`, explicit `predecessor`, and `tombstone` on removal.
- **`(item_version, parser, parser_version)` → `extracted_text`:** Parsed text and structural metadata (`blocks`, `tables`, `metadata`) are stored per parser and computed parser version, and indexed in SQLite FTS5 for phrase and boolean search without a model call.
- **Drift classification:** `text_hash` is compared only under the same parser and `parser_version`. If a file's `bytes_hash` changes (for example, a PDF title property is touched) while its `text_hash` under the same parser stays identical, ingest/parse classifies the change as `metadata_only` rather than content drift. Upgrading a parser writes new `extracted_text` rows without misattributing the difference to the upstream source.
- **Generic graph tables (`entities` and `edges`):** Core owns the table shapes and item-level provenance; recipes own domain meaning and populate them during analysis stages.

---

## 3. Structural Reconnaissance & Honest Parsing

Before any recipe stage runs, Commissure evaluates whether each input file can actually be read:

- **`commissure probe`:** Inspects files directly on disk without creating or locking a workspace. Reports file counts, extensions, tabular header/row shapes (`possible_title_row`, `ragged_rows`, uncached formulas), DOCX heading hierarchies (`heading_level_skips`, `possible_unstyled_headings`), and per-page PDF readability verdicts.
- **Per-page PDF readability gate:** Judges every PDF page from document structure rather than character count alone—checking font encodings, `/ToUnicode` CMaps, Adobe Glyph List mappings for Type 3 fonts, glyph decodability ratios, and image coverage (`image_only`, `image_dominant`, `no_text_layer`, `font_without_unicode_map`, `undecodable_glyphs`, `full_page_image_under_text`).
  - A sparse one-word title page with valid fonts passes as `readable`.
  - A scanned page with a short Bates stamp on top is rejected as `image_dominant`.
  - Pages that fail the gate are recorded with machine-readable reasons and never passed downstream as text.
  - Undecodable glyphs on otherwise readable pages become `U+FFFD` (`\ufffd`) with an `unmapped_glyphs` diagnostic—never raw `(cid:N)` strings or control bytes.
- **Fast default parsers + opt-in `ocr`:** `commissure run --step parse` runs the default parsers (`pdfplumber`, `docx`, `xlsx`, `csv`, `json`) in a bounded local process pool. Running `commissure run --step parse --parser ocr` invokes a locally installed Tesseract binary on pages the gate rejected, emitting `warning` text with per-page `ocr` flags, orientation detection (`ocr_rotated_180`), and confidence scores. Stages opt into OCR text explicitly via `Stage(..., text=("ocr", "default"))`.

---

## 4. Stage Execution, Outcomes & Staleness

A recipe (`solutions/<name>/recipe/recipe.py`) defines `STAGES`, an ordered list of [`Stage`](../commissure/recipes.py) declarations.

### Startup Validation
Before touching the workspace, `load_recipe` validates the recipe package and fails immediately on:
- Missing or empty `STAGES`
- Any stage `schema` that does not set `ConfigDict(extra="forbid")`
- `needs` references that do not name an earlier stage of compatible subject kind
- Unknown parser names or non-JSON stage parameters

### Three Stage Subjects (`item`, `group`, `run`)
- **`item`:** Receives one [`Item`](../commissure/recipes.py) with its read-only blob `path`, extracted `text` and `structure`, and validated `upstream` payloads. Items missing parsed text or valid current upstream payloads are recorded automatically as `skipped` (`no_text`, `text_stale`, `upstream_missing`, `upstream_not_valid`, `upstream_out_of_date`) rather than passed incomplete to stage code.
- **`group`:** Partitions complete items via `group_by(item)` and receives [`Group(key, items, excluded)`](../commissure/recipes.py).
- **`run`:** Executes once across the workspace and receives `Group("run", items, excluded)`, explicitly listing every incomplete item and why it was excluded.

### Four Explicit Outcomes
Every subject touched by a stage receives one of four outcomes in the ledger:
1. **`valid`** — Payload validated against the stage's strict Pydantic `schema`.
2. **`warning`** — Validated payload recorded alongside non-fatal diagnostics (for example, OCR origin or a soft domain anomaly).
3. **`quarantined`** — No record written; reason code and field-level diagnostics recorded in `outcomes` / `outcome_issues`.
4. **`skipped`** — Subject was not eligible to run (for example, `no_text` or `upstream_not_valid`).

`latest_seen` and `latest_valid` remain separate in the database views (`current_outcomes`, `current_records`): if a new version of an item is quarantined, prior valid records are not destroyed, and any fallback is explicitly marked `stale` rather than hidden.

### Checkpointing, Resumability & Acceptance Gates
- Stages checkpoint per subject in SQLite. Interrupted runs or slice iterations (`--limit N`) resume automatically on the next invocation when the recipe fingerprint and parameters match; `--fresh` forces a new snapshot.
- Core fails closed when discovery is empty, nothing is attempted, or 100% of attempted subjects are quarantined (unless explicitly overridden on the `Stage`). All items `unchanged` succeeds cleanly as `noop`.
- Any stage may also supply `gate(counts) -> list[str]` to enforce domain-specific thresholds over `discovered`, `valid`, `warning`, `quarantined`, and `skipped`.

### Automatic Staleness Fingerprinting
Each completed stage run is an append-only snapshot. Core computes a stage fingerprint (`stage_fp`) from the stage declaration, the JSON Schema of its Pydantic model, `Stage.assets`, and the AST of every recipe module reachable through transitive relative imports (`from . module import ...`):
- Editing one stage's code marks only that stage's earlier outputs `code_stale` in `current_outcomes` / `current_records` without staling upstream stages or requiring manual version bumps.
- Parameter changes, upstream record updates, and new item versions are tracked separately as `params_stale`, `input_stale`, and `version_stale`.

---

## 5. Deterministic First & The `ctx.llm` Seam

Schemas, coercions, and extraction rules are written in plain Python and Pydantic first. `ctx.llm` ([`commissure/llm.py`](../commissure/llm.py)) is reserved for unstructured residue that requires judgment:

- **Offline by default:** Remote model providers are refused unless the stage configuration explicitly sets `allow_remote=True`. Local model servers are probed before execution starts (`probe_local_model`) so a missing server fails immediately with a clear diagnostic rather than retrying silently.
- **Content-addressed caching before domain validation:** Raw model responses are cached in `llm_cache` before recipe-level domain validation so tightening a downstream check does not re-bill or re-run the model. Every call is logged in the `llm_calls` ledger with token counts, latency, and status.
- **Tolerant boundary, strict schema:** A tolerant coercion boundary handles common model formatting quirks before validating strictly against the stage's Pydantic model (`extra="forbid"`). Transient errors retry with backoff; systematic failures trip a circuit breaker.

---

## 6. Human-Verifiable Results: `Evidence` & `Review`

Every extracted result must be checkable by a human against the original document without reading code.

### Verified `Evidence` Pointers
Recipes embed the core [`Evidence`](../commissure/evidence.py) type in their Pydantic payloads. An `Evidence` object is a verified pointer, not an unverified quote:
- It names the `item_version_id`, a recipe-defined `kind`, and (whenever quoting extracted text) a half-open character span `[start, end)` into the exact `extracted_text` row supplied to the stage.
- When the stage outcome is committed, core verifies that the subject owns the item, the `extracted_text_id` matches, the offsets are in bounds, and the text slice equals the claimed quote. A mismatch fails the run immediately as a recipe bug.
- Core—never the recipe—resolves the 1-based page number, OCR origin (`via_ocr`), and OCR confidence from the block structure and writes them into the stored `Evidence` and the `evidence` / `evidence_review` tables.
-Stages consuming upstream records may copy an upstream `Evidence` unchanged. When a recipe cleans or decodes text before quoting (such as HTML entities inside a JSON field), `Evidence` points at the item as a whole (`start=None, end=None`) and says so in `kind` rather than guessing offsets.

### The Verdict Log (`commissure review` & `/review`)
A stage declares review questions over its payload fields via [`Check(pattern, question)`](../commissure/review.py), where `pattern` is a JSON Pointer pattern (`"/quotes/*"`); core also exposes parse checks (`parse:unreadable_pages`, `parse:ocr_pages`).
- **Append-only originals:** Samples, sample instances, and human verdicts (`correct`, `wrong`, `unclear`) are stored in append-only tables protected by SQLite triggers against `UPDATE` and `DELETE`.
- **Seeded random sampling:** Only seeded random samples (`commissure review sample --check <id> --n <N>`) from a complete, non-stale stage snapshot count toward an error rate. Ad-hoc spot checks (`review spot`) are recorded and reported, never rated.
- **Error rates & confidence intervals:** Each sample's error rate is `wrong / (correct + wrong)`, always reported alongside `n` and its Wilson 95% confidence interval.
- **Publishing rates into deliverables:** A stage with `reads_review=True` includes the plan-time review snapshot in its `input_fp` and reads formatted summary rows via `ctx.review_report()`. Recording a new verdict marks that deliverable stage `input_stale`.
- **Browser review interface (`/review`):** Mounted automatically beside any recipe `INTERFACE` under `commissure serve`, displaying the check question, highlighted span in context, and the original ingested blob under `Content-Security-Policy: sandbox` (PDFs, raster images, plain text, and script-free collapsible `<details>` JSON trees inline; active formats like HTML/SVG forced to download).

---

## 7. Three-Tier Repository Layout

```text
commissure/            Core substrate (storage, probe, parsers, runner, llm, evidence, review, serve, cli, api.py)
commissure/patterns/   Graduated reusable stage helpers (locate_offset, write_record_table)
commissure/scaffold/   Solution template written by `commissure init`
commissure/tools/      Release & license lifecycle tooling (`commissure release`, `commissure licenses`)
solutions/<name>/      Independent per-operation git repositories (gitignored by core)
docs/                  Permanent architecture, operator, and contributor documentation
tests/                 Production-schema tests, probe/parse fixtures, goldens, demo recipe
.agents/skills/        Packaged coding-agent workflows (symlinked in .claude/skills/)
AGENTS.md              Authoritative rules, vocabulary, and verification discipline
```

1. **`commissure/` (Core substrate — kept small):** Exposes [`commissure/api.py`](../commissure/api.py) as the single import surface for recipes. Never names a client, corpus, or business document type.
2. **`commissure/patterns/` (Graduated helpers):** Standalone utilities governed by the **Rule of Two**—helpers start inside a single recipe and graduate here only when a second recipe needs them.
3. **`solutions/<name>/` (Self-contained operations):** Each solution is its own git repository containing `recipe/` (the only code that ships to clients), `releases/`, `tests/`, `docs/`, `intake/`, and `workspace/`.
