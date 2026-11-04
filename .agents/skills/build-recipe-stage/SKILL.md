---
name: build-recipe-stage
description: >-
  Build or extend one stage, HTML deliverable, or web interface (INTERFACE) of a
  Commissure recipe after recipe/AGENTS.md is approved: schema, function,
  declaration, slice run, proven-to-fail tests, then stop. Use when asked to
  implement, build, add, or extend a stage, schema, extraction, analysis,
  deliverable, dashboard, or local web interface in a solution.
---

# Build a recipe stage

Root `AGENTS.md` wins on every rule. This skill
is the order of work for one stage; do not restate those rules in prose.

## Precondition

1. `solutions/<name>/recipe/AGENTS.md` must be approved by the operator (or the operator
   names the stage to build). If it is missing or only a scaffold, stop and run
   `investigate-solution` first.
2. Build the first unbuilt row of `## Proposed stages`, or the stage the operator names.
   Block on unresolved `## Open questions` that affect that stage.
3. Investigate used `/tmp/commissure-recon-<name>` only. If `solutions/<name>/workspace/`
   lacks the needed ingest/parse for this stage, do those here before the stage run.

## One stage, then stop

A stage is done when: schema + function + `Stage(...)` exist; a slice was inspected;
each quarantine/warning rule for this stage has a proven-to-fail test; the run (full or
approved slice) is reported; measured counts in Acceptance gate are refreshed. Then
**stop**. Do not start the next stage in the same turn unless the operator named several.

Hand off in a fixed short message: stage built, outcome counts, quarantine reasons seen,
tests that proved failure, and “next stage `<name>`, or change?”

## Workflow

Prefer scaffold modules (`extract.py`, `analyze.py`, `deliver.py`) while the stage is
small. Split to a domain-named module when it outgrows them. Keep models in `schema.py`
until a split is clearly needed.

1. **Schema (`recipe/schema.py`)**: Pydantic model with `extra="forbid"` (core enforces at
   load). Missing values use `None` plus a diagnostic—never a fabricated default. A
   field that points at inputs is core `Evidence` (`commissure.api`), never a
   recipe-local model: build it with `Evidence.span(item, start, end, kind=...)`,
   `.on_page(item, page, kind=...)`, or `.whole_item(item, kind=...)` (text the recipe
   cleaned or decoded) from an `Item` the stage received with `text=` declared. Never
   set `locator`, `page` on a span, `via_ocr`, or `confidence`: core resolves them when
   the record is stored, and a pointer that does not check out fails the run. Evidence
   copied unchanged from an upstream record is accepted. Declare the questions a reviewer
   should ask of the stage's results as `checks=(Check(pattern, question),)` on `Stage`
   (one thing per question; the operator settles the wording before any sample).
2. **Stage function**: Import only from `commissure.api` (and `commissure.patterns` if
   present). Return the schema instance or `RecordEnvelope.warning` / `.quarantined` /
   `.skipped` with a non-empty `reason_code` from `recipe/AGENTS.md`.
   - Check `item.upstream_outcomes` / `group.upstream_outcomes` when upstream warnings
     (`ocr_pages`, `unreadable_pages`) affect trust.
   - Inspect `group.excluded` on group and run stages so incomplete items are never
     silently omitted. `group_by` must place from the locator (or item fields always
     present); if it reads upstream payloads, incomplete items stay unplaced.
   - `text=("ocr", "default")` falls back silently when OCR was not run. OCR-sourced text
     (`item.parser == "ocr"`) is lower trust: surface as `warning` / `via_ocr` with page
     confidence (Normalization 6).
   - For `ctx.llm`: set `params` to `model` (`provider:model`; local servers via
     `base_url`) and `context_tokens` (the model's real window; required, core has no
     model table). Declare the input slice explicitly (never `text[:N]`); catch
     `ExtractionFailed` and `InputTooLarge` into `quarantined`. Keep deterministic stages
     free of LLM `params` so Phase A runs without a model server.
   - For deliverables (`ctx.outputs`): verify structural validity and zero external URLs
     before returning the output record.
3. **Declare (`recipe/recipe.py`)**: append `Stage(...)`. Field semantics live on `Stage`
   in `commissure/recipes.py` and `commissure.api`. Always set what this stage needs:
   `subject`, `schema`, `needs`, `roles`, `text`, `group_by`, `assets`, `gate`, `params`,
   `allow_empty` as applicable.
   - List every template and `brand/*` file the stage reads in `assets=` (fingerprint /
     staleness).
   - A stage’s `needs` may never include a run stage. If a later stage needs an aggregate,
     use a one-group group stage (`group_by` → `lambda _: "whole_corpus"`).
4. **Iterate on a slice** in `solutions/<name>/` (`uv run --directory solutions/<name>` when running from the repo root). If the workspace already has data,
   start with `inspect`, not a fresh ingest. Commands: `commissure/cli.py`. Typical loop:
   ```bash
   uv run --directory solutions/<name> commissure run --step <stage> --limit 5 --json
   uv run --directory solutions/<name> commissure inspect --run latest --stage <stage> --json
   uv run --directory solutions/<name> commissure inspect --only-errors --limit 10 --json
   uv run --directory solutions/<name> commissure inspect --subject <key_or_locator> --json
   uv run --directory solutions/<name> commissure inspect --call <call_id> --json   # LLM: raw request + response
   ```
   `--limit N` takes the first N pending subjects—also hit quarantine cases from
   `recipe/AGENTS.md` via `--subject` or a targeted fixture. Verify counts against
   `discovered` (zero successes = failed run; a suspiciously all-valid run is also wrong).
   Local LLM stages default to `workers=1`; code edits start a fresh snapshot; otherwise a
   `--limit` or interrupted run resumes (`--fresh` forces a new snapshot).
5. **Tests (`solutions/<name>/tests/`)**: use `open_workspace` / the CLI—never hand-rolled
   tables. Patterns to copy: rule input + clean sibling
   (`test_every_rule_fires_on_its_input_and_not_on_clean_siblings`), gate fail,
   honest empty state, external URL refused, mock LLM transport, `needs_tesseract` when OCR
   is required. Name each test in `recipe/AGENTS.md` Quarantine rules.
6. **Verify**: deterministic stages—run the full corpus. LLM stages—slice, read
   `inspect --call`, then ask before a long full run. A gate that fails on purpose is a
   reported result, not a bug to fix. From the repo root: `uv run pytest`,
   `uv run ruff check .`, `uv run ruff format --check .`. Report commands and results
   verbatim.
7. **Document**: short comment at any non-obvious choice, guarded by a test. Refresh
   measured counts in Acceptance gate only (thresholds stay operator-approved). Update the
   solution `README.md` stage list when commands change. Leave `CHANGELOG.md` /
   `release.toml` / targets to `release-target`. Raise a wrong-seeming rule with the
   operator—do not work around it.

## HTML deliverables

A deliverable is a `run` or `group` stage that writes under `ctx.outputs` and returns a
record describing the files. It is not an interface.

- Fragments only via `render_page` (`commissure.api`); never a custom `<!DOCTYPE html>`
  shell or `pico.min.css` without `report.css`. Contract: `commissure/deliver/html.py`.
- Read the header comment and `:root` tokens at the top of
  `commissure/deliver/static/report.css` first. Do not override core desktop typography or
  sticky header controls. Page-local `<style>` may add layout classes when no primitive
  fits; tokens only—no hard-coded color palettes—and say so in the report.
- Form labels and `.field-hint` above `.form-row`; table filters in `.section-toolbar`
  (details in the `report.css` header). Concise `thead th` labels.
- Branding only in `recipe/brand/{brand.css,header.html,footer.html,logo.svg}`. List brand
  files in `assets=`.

## `INTERFACE` (not a stage)

Optional `INTERFACE = Interface(...)` in the recipe, mounted by `commissure serve`. It
renders records and produces no outputs. Do not run `commissure run` for it.

- Core owns the chrome; `render` returns a body fragment. Actions use `ActionContext` /
  `ActionError`; mask secrets; include the CSRF field on forms. Empty state looks empty.
- Put the mount in `recipe/interface.py` and `templates/interface.html.j2`, and test
  against the real HTTP server (CSRF, actions, layout). If core is missing a public helper,
  stop and propose it—do not call private `ctx._*` APIs.

## Boundaries

- Keep solution changes inside `solutions/<name>/recipe/` and `solutions/<name>/tests/`.
  Never leave uncommitted local edits in `commissure/` when finishing a solution task (verify
  `git status --short` at the `commissure` root is clean). If a task uncovers a core bug or
  needs a domain-neutral substrate change, stop and propose a standalone, client- and
  domain-neutral PR to `commissure` tested in `tests/`.
- Keep helpers in the recipe until a second recipe needs them (Rule of Two).
- Keep `intake/` read-only, `allow_remote=False` unless approved in `recipe/AGENTS.md`, and
  run no git commands unless asked.
