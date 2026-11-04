# Commissure Agent Playbook (`AGENTS.md`)

This file is the single source of truth for Commissure's rules, vocabulary, and structural invariants. Every invariant here applies without exception.

When code does something that looks arbitrary (a threshold, a refusal, a lock), the comment beside it says why, and a test fails if it is undone: read both before changing it. When you make such a choice, leave the same: a short comment at the site and a test. Docs describe the current state only; history is in git.

**When a task appears to require violating a principle, stop and say so.** Report the conflict, the principle, and the options. Never quietly deviate, work around a rule, or reinterpret it to fit the task.

---

## 1. Vocabulary & Repository Boundaries (The Three Tiers)

- **Vocabulary:** Use these exact nouns without inventing synonyms: **Solution** (`solutions/<name>/`), **Target** (`solutions/<name>/releases/<target>.toml`), **Release** (`build/releases/<target>/` + `solutions/<name>/releases/<target>-v<version>.tar.gz`), **Recipe** (`solutions/<name>/recipe/`), **Workspace** (`state.db`, `blobs/`, `outputs/`), **Source** (a registered input folder), **Item** (one unit of input: file, JSON object, row), **Locator** (item address within its source), **Role** (recipe-defined label on items, e.g., `corpus`, `reference`, `comment`), **Stage** (one recipe step over one **Subject** kind: `item`, `group`, or `run`), **Run** (one stage invocation), **Outcome** (`valid`, `warning`, `quarantined`, or `skipped`, with diagnostics), **Record** (validated typed payload for a subject), **Evidence** (a verified pointer from an outcome into an item version: optionally a page, optionally an exact character span of one extracted text row; core owns its shape and checks it, recipes say what it supports), **Check** (a declared question about one kind of result: `Check(pattern, question)` on a stage over payload fields, or a core parse check), **Sample** (a seeded random draw of a check's instances from one complete, fresh snapshot), **Verdict** (a reviewer's `correct`, `wrong`, or `unclear` on one instance), **Entity** / **Edge** (rows in the generic graph tables), **Output** (a file written to `outputs/` described by a record; an interface renders records and is not an output).
- **`commissure/` (Core Substrate — Keep Small):**
  - Owns storage (`state.db` in SQLite `WAL` mode + sharded content-addressed `blobs/` + `outputs/`), `(source, locator)` → `item_version` versioning (`bytes_hash` for byte identity, `text_hash` per `(item_version, parser, parser_version)` for content drift, tombstones on removal, `latest_seen` kept separate from `latest_valid`), generic `entities` and `edges` tables (shape and provenance in core; meaning in recipes), per-page readability gates and parsers (run in a bounded local process pool; opt-in `ocr` parser over local Tesseract emitting `warning` text with per-page confidence), `RecordEnvelope` and outcome ledger, bounded thread-pool runner with per-item SQLite checkpointing and `_CONN_LOCK` (main thread commits outcomes; worker threads touch SQLite only through `ctx.llm`), FTS5 search, the single `ctx.llm` seam (`commissure/llm.py`: `extract` and `complete`, content-addressed `llm_cache` cached before domain validation, `llm_calls` ledger, tolerant coercion boundary, transient retries, and systematic-failure circuit breaker), CLI inspection (`commissure/inspect.py`, `commissure/recon.py`), the verdict log (`commissure/review.py`, `commissure review`), offline HTML shell (`commissure/deliver/`), and single-process HTTP mount (`commissure/serve.py`), which mounts only a recipe's `INTERFACE`: core adds no web routes without one. Beside an interface, core owns the review page (`commissure/review_page.py`, `/review/*`): the verdict log's interface over the same functions as `commissure review`, with originals served from blobs under `Content-Security-Policy: sandbox`, only inert types (PDF, raster images, plain text, and JSON as a script-free collapsible tree) shown inline, and every other type a download.
  - **Human-verifiable results:** Every result must be checkable by a person against the original without reading code (`commissure/evidence.py`, the `evidence` table, the `evidence_review` view). Evidence is a pointer, not a copy: it names the item version and, as narrowly as the stage can say, a page and a half-open span `[start, end)` of the one extracted text row the stage was given, plus a recipe-defined `kind`; core records it against the outcome with the JSON Pointer of the payload field it sits in. Core checks every span when the outcome is written (the subject owns the item, the text is the one supplied, the offsets are in range, the slice equals the claimed text) and fails the run on a mismatch, because that is a recipe bug, not bad data. Core, never the recipe, resolves page, OCR origin, and OCR confidence, and writes them into the payload's `Evidence` so downstream stages and deliverables can show them. A stage may also carry an `Evidence` copied unchanged from an upstream record it consumed; any other evidence that does not check out against its own subject is rejected. When the recipe cleaned or decoded the text it quotes (HTML in a JSON field), evidence points at the item without a span and says so in its `kind`; it never points at a near match. Recipes embed the core `Evidence` type in their payloads and never define their own. Core findings are evidence too: parse issues that name pages are exposed as page evidence read from their recorded diagnostics. Evidence proves location, not meaning: whether it supports a paraphrased claim or a model's judgment is the reviewer's call. Evidence belongs to its run's snapshot and is never modified, and each resolves to something a person can open: the original's source path, the stored blob, the page.
  - **Verdict log:** Verdicts are human input, so they are originals, never derived data: samples, instances, and verdicts are append-only (triggers refuse update and delete), and the latest verdict on an instance counts. Only seeded random samples count toward an error rate; spot checks are recorded and reported, never rated. A rate is wrong / (correct + wrong) for one sample, always shown with its n and Wilson 95% interval. An earlier verdict on an identical instance is shown, never counted, until the reviewer records it again. A check's question stays out of the stage fingerprint, because asking about a result does not change it. A stage with `reads_review=True` takes the plan-time review snapshot into its input fingerprint, and `ctx.review_report()` returns that snapshot so a deliverable can publish the rate; a new verdict makes that stage stale. Verdict writes are the one exception to "a writer holds the workspace lock": they touch only the review tables, are append-only, and rely on SQLite's own write locking, so review continues during long runs. Verdicts live in `state.db` only; rebuilding a workspace loses them.
  - **Surface:** Recipes import only from `commissure/api.py` (and optionally `commissure/patterns/`), never from internal core modules or provider SDKs. CLI commands and exit codes live in `commissure/cli.py`.
  - **Hard rules:** Core never references a client, corpus, or business document type. Core runtime dependencies and vendored assets are permissive (Section 3.8). Scheduling belongs to the OS (`cron`, `launchd`, `systemd`), supported by workspace lockfiles, `--trigger`, and deterministic exit codes (`0` ok/noop, `1` failed, `2` usage/config error, `3` locked).
- **`commissure/patterns/` (Graduated Reusable Helpers):**
  - **Rule of Two:** Keep helpers inside a recipe first; graduate to `commissure/patterns/` only when a second recipe needs them or the operator asks.
- **`commissure/tools/` (Release & Repository Lifecycle Tooling):**
  - Holds developer and delivery tooling that packages, audits, or manages solution and release trees outside the runtime pipeline (`commissure/tools/release.py`, mounted as `commissure release`; `commissure/tools/licenses.py` with its register `licenses.toml`, mounted as `commissure licenses`). Never imported by recipes (`commissure/api.py`) or by runtime core modules, never touches `state.db`, and stays domain- and client-neutral. Reusable stage helpers go in `commissure/patterns/`; domain-specific pre-ingest fetchers go in `solutions/<name>/recipe/tools/`.
- **`solutions/<name>/` (Self-Contained Operations):**
  - Each solution is an independent git repository checked out under `solutions/<name>/` (`solutions/*` is gitignored by core except `solutions/.gitkeep`). Holds `README.md`, `.gitignore`, `CHANGELOG.md`, `release.toml`, `releases/` (client targets `<target>.toml` and published tarballs), `docs/` (indexed in `docs/README.md`), `intake/`, `recipe/` (the only code that ships; optional `recipe/tools/` holds pre-ingest fetchers/scripts that populate `intake/` and read `.env`), `tests/`, and `workspace/`. Scaffolded exclusively via `commissure init solutions/<name>` from `commissure/scaffold/template/`.
  - Recipe code, docs, releases, and tests are tracked in that solution's repository; `intake/` and `workspace/` are local unless a solution's `.gitignore` opts in (with binaries in Git LFS via `.gitattributes`). SQLite side files (`-wal`, `-shm`, `-journal`) and `workspace.lock` are never tracked.
  - **Solution vs. Core Boundary (Upstreaming PRs):** When working on a solution, keep all domain schemas, extraction rules, and first-use helpers inside `solutions/<name>/recipe/` (**Rule of Two**) and verify `git status --short` at the `commissure` root is clean before finishing so local core edits are never left stranded outside the solution's repository. If a task uncovers a core bug or requires a domain-neutral substrate change, stop and tell the operator before editing `commissure/`. Contributors and client agents upstream core changes via standalone pull requests to `commissure` that contain zero client, office, solution, or corpus names (`scan_forbidden`) and include proven-to-fail synthetic tests in `tests/`.

---

## 2. Verification & Engineering Discipline

1. **Prove every check can fail:** Before trusting a new test, validation rule, or data-quality metric (coverage, lineage, completeness, recency), run it with the fix reverted or against a malformed input and confirm it fails for the expected reason.
2. **Verify against the tree and database:** Inspect the actual filesystem tree, database rows, and command exit codes before reporting completion; never trust prior summaries or docs over code.
3. **Equivalence before deletion:** Before deleting or replacing an existing code path, parser, or query, run an equivalence check proving matching (or intentionally improved and reviewed) outputs on real or golden fixtures.
4. **Test the production schema path:** Tests must initialize and migrate SQLite workspaces through `open_workspace`—never hand-rolled tables.
5. **Verify output artifacts structurally:** Tests and stages writing files (`.docx`, `.xlsx`, `.pdf`, `.html`, `.csv`) must assert structural validity (file exists, non-zero size, expected page/sheet/row counts, zero external CDN URLs)—never just return status.
6. **Zero successes is a failure:** After any run, compare `valid`, `warning`, `quarantined`, and `skipped` against `discovered`. Discovering nothing, attempting nothing, or failing 100% of attempted items fails the run (unless explicitly permitted per stage); all items `unchanged` succeeds as a clean `noop`.

---

## 3. Data Sovereignty, Offline & Operational Safety

1. **Offline by default:** Never embed external CDN `<script>`, `<link>`, or font URLs in HTML templates or recipe UIs. Use vendored local assets exclusively; empty states must look empty, never placeholder data.
2. **Explicit remote egress:** Never invoke a non-local model/API unless the stage configuration explicitly sets `allow_remote=True`.
3. **Originals are read-only:** Never modify source input files. Write outputs to `workspace/outputs/`.
4. **No silent truncation or silent defaults:** Never slice item text with `text[:N]` to fit a prompt without explicit chunking/slicing semantics, and never substitute fallback values when extraction or model calls fail—emit `warning` or `quarantined` diagnostics instead.
5. **Secrets:** Secrets come from the environment; a solution may keep a gitignored `.env` loaded by its own tools, never shared across solutions and never read by core. Secret values are masked in logs, diagnostics, ledgers, and reports.
6. **Git hygiene:** Do not stage, commit, branch, or push git changes unless the user explicitly asks.
7. **No LaTeX in Markdown:** Write math and symbols as plain text or Unicode (`n^2`, `→`). Never use `$...$`, `$$...$$`, `\(...\)`, or `\[...\]`.
8. **Third-party licenses:** Government clients approve permissive licenses without review, so prefer them. Core runtime dependencies and vendored browser assets use only the licenses allowed in `commissure/tools/licenses.toml` (MIT, BSD, Apache-2.0, ISC, and kin). Weak copyleft (LGPL, MPL) enters only as an optional extra that `commissure/` never imports (`commissure[docx]`), with a recorded exception. Strong copyleft (GPL, AGPL), source-available (SSPL, BUSL), and unlicensed code never enter. Before adding or upgrading a dependency or vendored file, check its license, including native libraries bundled in binary wheels (`*.dist-info/licenses/`), which package metadata does not describe; record anything outside the allowlist as an exception, and only on the operator's decision. `tests/test_licenses.py` enforces the register. Answer license questions ("what non-MIT licenses do you use?") by running `commissure licenses` (`--json`) and citing its exceptions, never from memory or raw metadata.

---

## 4. Skills & Recipe Workflow

Recurring workflows live in `.agents/skills/<name>/SKILL.md` (with relative symlinks in `.claude/skills/`). Read the matching skill before starting:

| Task | Skill | Core rules |
| --- | --- | --- |
| Scaffold a new solution | `new-solution` | Run `commissure init solutions/<name>`, hand off the operator checklist, and stop. |
| Reconnaissance & `recipe/AGENTS.md` | `investigate-solution` | Read `docs/README.md`, run `probe`, parse a `/tmp` scratch workspace (plus `--parser ocr` when needed), run `recon`, and complete `recipe/AGENTS.md` before writing stage code. |
| Build or extend a stage, deliverable, or `INTERFACE` | `build-recipe-stage` | Express schemas (`extra="forbid"`) and deterministic extraction first; use `ctx.llm` only for residue requiring judgment. Iterate with `--limit` and `--json` (`workers=1` default on local LLMs), inspect via `commissure inspect`, and write proven-to-fail tests. |
| Ship a release to a client | `release-target` | Propose version bumps and client-facing `CHANGELOG.md` entries, build via `commissure release`, review the diff for leaks, publish tarball, and `--push` only when explicitly told. |

---

## 5. Tests and Fixtures

- Environment: `uv sync`
- Tests: `uv run pytest` (single file/test: `uv run pytest tests/test_ingest.py::test_name`)
  - Tests run in parallel by default (`-n auto` in `pyproject.toml`); add `-n0` to see `print` output or use `pdb`.
- Lint & format: `uv run ruff check .` and `uv run ruff format --check .` (apply with `uv run ruff format .`)
- A report may say "tests pass" only by naming the command it ran and its result.

Rules:
1. Core tests live in `tests/` (`ws` / `ws_root` and `cli` fixtures in `tests/conftest.py`). Input fixtures in `tests/fixtures/` are copied into `tmp_path` before use and never modified in place; generated fixtures (`tests/fixtures/make_probe_corpus.py`) are never hand-edited.
2. Real samples go in `tests/fixtures/real_samples/` with a human-verified entry in `tests/fixtures/real_samples.expected.json` (`tests/test_real_samples.py`).
3. Parse goldens live in `tests/golden/parse/`; regenerate with `COMMISSURE_UPDATE_GOLDEN=1 uv run pytest tests/test_parse_golden.py` and review `git diff tests/golden` line by line.
4. Runner behaviour is tested with `tests/recipes/demo/`, picking subjects by locator rather than commit order.
5. Solution tests and fixtures live in `solutions/<name>/tests/` (outside `recipe/`) and are collected automatically by `pytest` (`testpaths = ["tests", "solutions"]` in `pyproject.toml`).

---

## 6. Delivery

Clients receive generated single-solution releases (`build/releases/<target>/` and `solutions/<name>/releases/<target>-v<version>.tar.gz`), never this repository. `solutions/<name>/releases/<target>.toml` names a client destination for its enclosing solution; the solution's `release.toml` gives its `version`, `include`/`exclude` allowlist (default `recipe/` and `CHANGELOG.md`), and client README text. `commissure release build <target>` refuses on uncommitted changes, failing tests, a `TODO` in `release.toml`, changed solution files without a version bump and `CHANGELOG.md` entry, an already-released version, or any shipped file (including the vendored wheel) matching built-in credential/path patterns, `solutions/.forbid.toml`, another solution's name, or the target's `forbid` patterns.
