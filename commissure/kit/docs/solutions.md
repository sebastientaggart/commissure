# Solution Developer Guide

A solution is a project of its own: a git repository with a `pyproject.toml` that depends on the `commissure` package ([`AGENTS.md`](../AGENTS.md), Vocabulary). For recipe code, CLI flags, graph tables, and review commands, see [`guide.md`](guide.md).

## 1. Lifecycle: Operator vs. Agent

0. **Try the example (5 minutes, no model)**
   - **Operator:** `uvx commissure new demo --example && cd demo && uv sync`, then the commands in its `README.md`: four steps, then `uv run commissure serve` for the results and `/review`.
1. **Start (`commissure new`)**
   - **Operator:** `uvx commissure new <dir>`, `cd <dir> && git init && uv sync`. Fills `docs/notes.md` and indexes reference docs in `docs/README.md`. The scaffolded recipe declares no stages and fails at startup on purpose.
2. **Collect & Curate (`intake/`)**
   - **Agent (when scripted download is needed):** Writes fetchers in `recipe/tools/` (Section 2).
   - **Operator / SME:** Runs fetchers or places files in `intake/<source>/`, checks manifests, and curates before ingest.
3. **Investigate (skill [`investigate-solution`](../skills/investigate-solution/SKILL.md))**
   - **Agent:** Probes `intake/`, parses a `/tmp` scratch workspace, runs `commissure recon`, fills the contract sections of `AGENTS.md`, and stops.
   - **Operator:** Reviews and approves the contract (`AGENTS.md`), resolves open questions, and approves thresholds and any `allow_remote=True`.
4. **Build (skill [`build-recipe-stage`](../skills/build-recipe-stage/SKILL.md))**
   - **Agent:** Builds one stage (or `INTERFACE`) at a time in `recipe/`, tests on a slice, adds proven-to-fail tests in `tests/`, updates the README's run commands, and stops.
   - **Operator / SME:** Inspects outputs, grades random samples in `/review` (`commissure serve` or `commissure review`), and approves the next stage.
5. **Upgrade core**
   - **Agent:** `uv lock --upgrade-package commissure && uv sync`, then `uv run commissure agent-kit` (replaces `.agents/commissure/` with the new version's rules, docs, and skills). Reads every **Upgrading** entry in core's `CHANGELOG.md` since the old version and acts on those that apply, then runs `uv run pytest`.
   - **Operator:** Reviews the diff (`uv.lock`, `.agents/commissure/`, the recipe changes) and commits.
6. **Release**
   - **Agent:** Proposes the `version` bump in `pyproject.toml` and the client-facing `CHANGELOG.md` entry, and runs `uv run pytest`.
   - **Operator:** Commits, tags `v<version>`, and pushes or hands the client the repository at that tag (`guide.md`, Section 5).

A core bug or a missing core feature is never patched in the solution's `.venv/`: the agent stops and drafts an upstream request ([`AGENTS.md`](../AGENTS.md), Boundaries). A contributor with a core checkout can try an unreleased core change against a solution without touching its lock: `uv run --with-editable ../commissure pytest` from the solution.

## 2. Fetchers (`recipe/tools/`) and `intake/` (Collect, Curate, Ingest)

- **Fetchers (`recipe/tools/`):** Pre-ingest scripts that download or import files into `intake/<source>/` and write `intake/manifests/<source>.json` (URL, timestamp, SHA-256, status). Optional API keys live in a gitignored `.env` (documented in `.env.example`) read only by the fetcher. Stages never import `recipe/tools/` or read `intake/` directly. Build fetchers on `commissure.patterns.fetch`, which supplies the fetch, save, and record rules: polite per-host spacing; a source stops at its first bot challenge (exit 3, resumable; exit 1 if any source failed), never posing as a browser; PDF or HTML only, never overwriting different bytes; each page's own PDFs under the site's document `path_prefix`, else the page as HTML; and `import_saved` for documents a person saves by hand when a site refuses scripts. A solution's fetcher lists only its sources.
- **Collect -> Curate -> Ingest:**
  1. **Collect** raw files into one subfolder per source (`intake/<source>/`).
  2. **Curate** before first ingest and after each refresh: an SME checks `intake/manifests/<source>.json` and moves out-of-scope or duplicate files to `intake/_excluded/<source>/`, logging each in `intake/_excluded/CURATION.md` (`YYYY-MM-DD  <source>/<file>  excluded: <reason>  (initials)`). Moving rather than deleting keeps the manifest entry so fetchers do not re-download excluded files.
  3. **Ingest** each curated subfolder separately (`uv run commissure run --step ingest --source intake/<source> --role <role>`), never `intake/` as a whole. Re-ingesting after moving a file out records a tombstone so outputs drop it.
  4. **Document** collection, manifest checks, curation, and processing for SMEs in the solution's `README.md` (or a runbook in `docs/`, indexed in `docs/README.md`).

## 3. Roles

Every `--step ingest` stamps a recipe-defined `--role <role>` (default `corpus`) on the folder's items: for example, `policy_document` (authorities), `clearance_package` (draft under review), and `context_report` (background reading). Stages set `Stage(..., roles=("...",))` so each step processes only the roles intended for it.
