# Commissure: rules for recipe authors

These are core's rules for anyone writing a solution on Commissure, and they bind core's own code too. Every invariant here applies without exception. A solution's own `AGENTS.md` and `recipe/AGENTS.md` add to them and never override them.

When code does something that looks arbitrary (a threshold, a refusal, a lock), the comment beside it says why, and a test fails if it is undone: read both before changing it. When you make such a choice, leave the same: a short comment at the site and a test. Docs describe the current state only; history is in git.

**When a task appears to require violating a principle, stop and say so.** Report the conflict, the principle, and the options. Never quietly deviate, work around a rule, or reinterpret it to fit the task.

In a solution, this file and its siblings live in `.agents/commissure/` (copied from the installed package; `VERSION` names it). They are core's: never edit them there. `uv run commissure agent-kit` replaces them after a core upgrade. Docs: `docs/guide.md` (recipe API, CLI, review), `docs/solutions.md` (lifecycle, operator vs. agent), `docs/architecture.md` (how core works). Where they name a core module (`commissure/cli.py`), read the installed code for exact behaviour: `uv run python -c "import commissure, os; print(os.path.dirname(commissure.__file__))"`.

---

## 1. Vocabulary

Use these exact nouns without inventing synonyms:

- **Solution:** a project that depends on the `commissure` package and holds one operation: `pyproject.toml`, `recipe/`, `tests/`, `docs/`, `intake/`, and a local `workspace/`. Created by `commissure new`, its own git repository.
- **Recipe:** the solution's `recipe/` folder: `recipe.py` declares `STAGES` (and optionally `INTERFACE`, `LIMITS`), `AGENTS.md` is its contract.
- **Workspace:** `state.db`, `blobs/`, `outputs/`.
- **Source:** a registered input folder. **Item:** one unit of input (file, JSON object, row). **Locator:** an item's address within its source. **Role:** a recipe-defined label on items (`corpus`, `reference`, `comment`).
- **Stage:** one recipe step over one **Subject** kind: `item`, `group`, or `run`. **Run:** one stage invocation. **Outcome:** `valid`, `warning`, `quarantined`, or `skipped`, with diagnostics. **Record:** the validated typed payload for a subject.
- **Evidence:** a verified pointer from an outcome into an item version: optionally a page, optionally an exact character span of one extracted text row. Core owns its shape and checks it; recipes say what it supports.
- **Check:** a declared question about one kind of result (`Check(pattern, question)` on a stage over payload fields, or a core parse check). **Sample:** a seeded random draw of a check's instances from one complete, fresh snapshot. **Verdict:** a reviewer's `correct`, `wrong`, or `unclear` on one instance.
- **Entity** / **Edge:** rows in the generic graph tables.
- **Output:** a file written to `outputs/` described by a record. An interface renders records and is not an output.

---

## 2. Results a Person Can Check

- **Human-verifiable results:** Every result must be checkable by a person against the original without reading code. Evidence is a pointer, not a copy: it names the item version and, as narrowly as the stage can say, a page and a half-open span `[start, end)` of the one extracted text row the stage was given, plus a recipe-defined `kind`; core records it against the outcome with the JSON Pointer of the payload field it sits in. Core checks every span when the outcome is written (the subject owns the item, the text is the one supplied, the offsets are in range, the slice equals the claimed text) and fails the run on a mismatch, because that is a recipe bug, not bad data. Core, never the recipe, resolves page, OCR origin, and OCR confidence, and writes them into the payload's `Evidence` so downstream stages and deliverables can show them. A stage may also carry an `Evidence` copied unchanged from an upstream record it consumed; any other evidence that does not check out against its own subject is rejected. When the recipe cleaned or decoded the text it quotes (HTML in a JSON field), evidence points at the item without a span and says so in its `kind`; it never points at a near match. Recipes embed the core `Evidence` type in their payloads and never define their own. Core findings are evidence too: parse issues that name pages are exposed as page evidence read from their recorded diagnostics. Evidence proves location, not meaning: whether it supports a paraphrased claim or a model's judgment is the reviewer's call. Evidence belongs to its run's snapshot and is never modified, and each resolves to something a person can open: the original's source path, the stored blob, the page.
- **Verdict log:** Verdicts are human input, so they are originals, never derived data: samples, instances, and verdicts are append-only (triggers refuse update and delete), and the latest verdict on an instance counts. Only seeded random samples count toward an error rate; spot checks are recorded and reported, never rated. A rate is wrong / (correct + wrong) for one sample, always shown with its n and Wilson 95% interval. An earlier verdict on an identical instance is shown, never counted, until the reviewer records it again. A check's question stays out of the stage fingerprint, because asking about a result does not change it. A stage with `reads_review=True` takes the plan-time review snapshot into its input fingerprint, and `ctx.review_report()` returns that snapshot so a deliverable can publish the rate; a new verdict makes that stage stale. Verdict writes are the one exception to "a writer holds the workspace lock": they touch only the review tables, are append-only, and rely on SQLite's own write locking, so review continues during long runs. Verdicts live in `state.db` only; rebuilding a workspace loses them. Backing them up belongs to the system owner, like scheduling (`docs/guide.md`, "Keeping Verdicts").

---

## 3. Boundaries

- **Public surface:** Recipes import only from `commissure.api` (and optionally `commissure.patterns`), never from internal core modules or provider SDKs. Tests may also use the internal modules these docs name for tests (`commissure.workspace.open_workspace`, `commissure.recipes.load_recipe`, `commissure.cli.app`). Needing any other internal import is a sign a core change is needed.
- **Rule of Two:** Keep domain schemas, extraction rules, and helpers inside the solution's `recipe/`. A helper graduates to `commissure/patterns/` only when a second recipe needs it or the operator asks.
- **Pre-ingest fetchers** (scripts that populate `intake/` and read `.env`) go in `recipe/tools/`.
- **Scheduling belongs to the OS** (`cron`, `launchd`, `systemd`), supported by workspace lockfiles, `--trigger`, and deterministic exit codes (`0` ok/noop, `1` failed, `2` usage/config error, `3` locked).
- **Core changes:** Never patch the installed `commissure` under `.venv/` to get unblocked. When a task needs a domain-neutral change in core, stop and tell the operator. Core is public, so a request names no client, agency, solution, or corpus, title included. Write the issue body to `docs/plans/upstream-<YYYY-MM-DD>-<short-title>.md` (gitignored) with these headings, and give the operator the command; never run it yourself, because an issue is public the moment it exists:
  - **Need:** what the solution can't do, in its own terms.
  - **Synthetic repro:** a minimal case.
  - **Proposed change:** an API sketch or behaviour (the maintainer may choose another design).
  - **Meanwhile:** the workaround kept in `recipe/` (Rule of Two), and what it costs.
  - **Core version:** `.agents/commissure/VERSION`.

  `gh issue create -R sebastientaggart/commissure --title "<title>" --body-file "docs/plans/<file>"`. Continue with the workaround, or stop. A contributor with a core checkout may instead open a pull request (`docs/contributing.md` in core's repository).

---

## 4. Verification & Engineering Discipline

1. **Prove every check can fail:** Before trusting a new test, validation rule, or data-quality metric (coverage, lineage, completeness, recency), run it with the fix reverted or against a malformed input and confirm it fails for the expected reason.
2. **Verify against the tree and database:** Inspect the actual filesystem tree, database rows, and command exit codes before reporting completion; never trust prior summaries or docs over code.
3. **Equivalence before deletion:** Before deleting or replacing an existing code path, parser, or query, run an equivalence check proving matching (or intentionally improved and reviewed) outputs on real or golden fixtures.
4. **Test the production schema path:** Tests must initialize and migrate SQLite workspaces through `open_workspace`, never hand-rolled tables.
5. **Verify output artifacts structurally:** Tests and stages writing files (`.docx`, `.xlsx`, `.pdf`, `.html`, `.csv`) must assert structural validity (file exists, non-zero size, expected page/sheet/row counts, zero external CDN URLs), never just return status.
6. **Zero successes is a failure:** After any run, compare `valid`, `warning`, `quarantined`, and `skipped` against `discovered`. Discovering nothing, attempting nothing, or failing 100% of attempted items fails the run (unless explicitly permitted per stage); all items `unchanged` succeeds as a clean `noop`.

A report may say "tests pass" only by naming the command it ran and its result.

---

## 5. Data Sovereignty, Offline & Operational Safety

1. **Offline by default:** Never embed external CDN `<script>`, `<link>`, or font URLs in HTML templates or recipe UIs. Use vendored local assets exclusively; empty states must look empty, never placeholder data.
2. **Explicit remote egress:** Never invoke a non-local model/API unless the stage configuration explicitly sets `allow_remote=True`.
3. **Originals are read-only:** Never modify source input files. Write outputs to `workspace/outputs/`.
4. **No silent truncation or silent defaults:** Never slice item text with `text[:N]` to fit a prompt without explicit chunking/slicing semantics, and never substitute fallback values when extraction or model calls fail: emit `warning` or `quarantined` diagnostics instead.
5. **Secrets:** Secrets come from the environment; a solution may keep a gitignored `.env` loaded by its own tools, never shared across solutions and never read by core. Secret values are masked in logs, diagnostics, ledgers, and reports.
6. **Git hygiene:** Do not stage, commit, branch, push, tag, or open issues/PRs unless the user explicitly asks.
7. **No LaTeX in Markdown:** Write math and symbols as plain text or Unicode (`n^2`, `→`). Never use `$...$`, `$$...$$`, `\(...\)`, or `\[...\]`.
8. **Third-party licenses:** Government clients approve permissive licenses (MIT, BSD, Apache-2.0, ISC, and kin) without review, so prefer them for any dependency a solution adds. Weak copyleft (LGPL, MPL) only as an optional extra the operator approves (`commissure[docx]`); strong copyleft (GPL, AGPL), source-available (SSPL, BUSL), and unlicensed code never. `uv run commissure licenses` reports core's own.

---

## 6. Lifecycle & Skills

Read the matching skill (`.agents/commissure/skills/`, linked in `.claude/skills/`) before starting:

| Task | How | Core rules |
| --- | --- | --- |
| Start a solution | `commissure new <dir>` (`--example` for a working one) | Hand the operator `docs/notes.md` and `intake/` to fill, and stop. |
| Reconnaissance & `recipe/AGENTS.md` | skill `investigate-solution` | Read `docs/README.md`, run `probe`, parse a `/tmp` scratch workspace (plus `--parser ocr` when needed), run `recon`, and complete `recipe/AGENTS.md` before writing stage code. |
| Build or extend a stage, deliverable, or `INTERFACE` | skill `build-recipe-stage` | Express schemas (`extra="forbid"`) and deterministic extraction first; use `ctx.llm` only for residue requiring judgment. Iterate with `--limit` and `--json` (`workers=1` default on local LLMs), inspect via `commissure inspect`, and write proven-to-fail tests. |
| Upgrade core | `uv lock --upgrade-package commissure && uv sync`, then `uv run commissure agent-kit` | Read every **Upgrading** entry in core's `CHANGELOG.md` since the old version (`https://github.com/sebastientaggart/commissure/blob/v<version>/CHANGELOG.md`) and act on those that apply before fixing tests by other means; then `uv run pytest`. |
| Release | bump `version` in `pyproject.toml`, `CHANGELOG.md` entry, tag `v<version>` | `uv run pytest` passes first; the repository at the tag is the release. Tag and push only when told. |

---

## 7. Tests

- Solution tests and small synthetic fixtures live in `tests/` (`uv run pytest`). Input fixtures are copied into `tmp_path` before use and never modified in place.
- Load the recipe with `load_recipe` (the production path) and build workspaces with `open_workspace` or the CLI (`commissure.cli.app` through `typer.testing.CliRunner`). The example solution's `tests/test_recipe.py` (`commissure new <dir> --example`) runs ingest, parse, and stages that way over a copy of its corpus.
- No test reaches outside the solution folder, and no fixture holds client data that may not be committed.
