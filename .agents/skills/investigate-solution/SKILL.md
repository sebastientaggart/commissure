---
name: investigate-solution
description: >-
  Reconnaissance for a populated Commissure solution before writing stage code.
  Reads docs/README.md and indexed notes, probes intake/, parses a scratch workspace
  and runs commissure recon for measurements, and completes recipe/AGENTS.md. Use when
  asked to investigate, assess, explore, or plan a solution whose inputs are in place.
---

# Investigate a solution

Goal: complete `solutions/<name>/recipe/AGENTS.md` for operator review before writing any stage code.

## 1. Read context

1. Root `AGENTS.md` (`../../AGENTS.md` when running inside `solutions/<name>/`).
2. `solutions/<name>/README.md`, `solutions/<name>/docs/README.md`, and every file in `docs/` except local scratch plans in `docs/plans/` or `docs/plan-*.md` (add missing index lines to `docs/README.md`).
3. If `docs/notes.md` exists and still contains `Replace this file before investigation.`, stop and ask the operator to fill it. If the file is absent (solutions scaffolded before that template), continue without requiring it.
4. Common patterns to consider: folder-level groups (`PurePosixPath(item.locator).parent.name`), `text=("ocr", "default")` when scanned PDFs exist, explicit `Group.excluded` handling on group and run stages, and trust-ordered extraction rules.

## 2. Reconnaissance

1. **Inventory `intake/`**: list subfolders, file types, counts, and sizes. If inputs sit directly under `intake/` beside `intake/README.md` instead of `intake/<batch>/`, note `--ignore README.md` and ask whether to move them into a batch subfolder.
2. **Probe every batch** (read the JSON, not just the summary):
   ```bash
   uv run commissure probe solutions/<name>/intake/<batch> --json
   ```
   Record per file type: unreadable pages and reasons, glyph flags (`unmapped_glyphs`, `U+FFFD`), DOCX heading/table topology, and CSV/XLSX encodings, delimiters, sheet schemas, and null rates.
3. **Parse a scratch workspace, then read recon.** When probe reports unreadable PDF pages, or when the folder is large enough that repeated lines and duplicate files are the question, ingest and parse into `--workspace /tmp/commissure-recon-<name>` (never `solutions/<name>/workspace/`). Run `parse`, then `parse --parser ocr` when unreadable pages were reported (needs the `tesseract` binary). Then:
   ```bash
   uv run commissure recon --workspace /tmp/commissure-recon-<name> --json
   ```
   Use the report as measurements: locator-parent counts, lines repeated across pages or files, identical `text_hash` sets under one `(parser, parser_version)`, table first-row signatures (`parsed_table`, or `probe_sheet` only when that sheet was not parsed), unreadable pages with gate reasons, and OCR pages whose `flags` contain `ocr` with `Block.confidence`. A missing OCR row on an unreadable page is `ocr not run`, never empty text. OCR text is lower trust (`0/O`, `1/I`, digit and punctuation slips). The report does not assign document roles, trust order, or quarantine rules.
4. **Spot-check originals** against probe, parse, and recon. Every discrepancy becomes a domain rule, a quarantine rule, or a test fixture.
5. **Split deterministic vs. LLM residue**: use regex, structure, and table topology first; reserve `ctx.llm` for genuine judgment residue and ask the operator if `allow_remote=True` is needed.

## 3. Fill `recipe/AGENTS.md` and stop

Complete every existing section (delete none):
- **Purpose**: operation, audience, outputs/interfaces, required extras (e.g. `commissure[docx]`).
- **Inputs**: sources (`intake/<batch>`), roles, locators, file types, probe and OCR findings citing filenames.
- **Domain rules**: each rule paired with the input that motivated it.
- **Quarantine rules**: every `warning` and `quarantined` condition, its `reason_code`, and the test name that will prove it fires.
- **Acceptance gate**: outcome count thresholds and any core fail-closed defaults tightened or relaxed.
- **Definition of done**: operator verification checklist before outputs ship.

Append `## Proposed stages` (stage name, subject, needs, roles, text, schema, one-line summary) and `## Open questions`. Stop and report findings for operator review—do not write `STAGES`, schemas, or stage code yet.
