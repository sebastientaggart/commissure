# Solution Developer Guide

Each solution is an independent git repository at `solutions/<name>/` ([`AGENTS.md` Section 1](../AGENTS.md)). For recipe code, CLI flags, graph tables, and review commands, see [`docs/guide.md`](guide.md).

## 1. Lifecycle: Operator vs. Agent

1. **Scaffold ([`new-solution`](../.agents/skills/new-solution/SKILL.md))**
   - **Agent:** Runs `commissure init solutions/<name>`, verifies the startup check, hands off the checklist, and stops.
   - **Operator:** Fills `docs/notes.md` and indexes reference docs in `docs/README.md`.
2. **Collect & Curate (`intake/`)**
   - **Agent (when scripted download is needed):** Writes fetchers in `recipe/tools/` (Section 2).
   - **Operator / SME:** Runs fetchers or places files in `intake/<source>/`, checks manifests, and curates before ingest.
3. **Investigate ([`investigate-solution`](../.agents/skills/investigate-solution/SKILL.md))**
   - **Agent:** Probes `intake/`, parses a `/tmp` scratch workspace, runs `commissure recon`, fills `recipe/AGENTS.md`, and stops.
   - **Operator:** Reviews and approves `recipe/AGENTS.md`, resolves open questions, and approves thresholds and any `allow_remote=True`.
4. **Build ([`build-recipe-stage`](../.agents/skills/build-recipe-stage/SKILL.md))**
   - **Agent:** Builds one stage (or `INTERFACE`) at a time in `recipe/`, tests on a slice, adds proven-to-fail tests in `tests/`, updates `docs/user-guide.md`, and stops.
   - **Operator / SME:** Inspects outputs, grades random samples in `/review` (`commissure serve` or `commissure review`), and approves the next stage.
5. **Release ([`release-target`](../.agents/skills/release-target/SKILL.md))**
   - **Operator:** Defines `solutions/<name>/releases/<target>.toml`.
   - **Agent:** Proposes the `release.toml` version bump and `CHANGELOG.md` entry, verifies `release.toml` `usage` matches `docs/user-guide.md`, runs `commissure release build <target>`, checks the diff for leaks, and builds the tarball.
   - **Operator:** Commits version bumps/tags and approves `--push`.

## 2. Fetchers (`recipe/tools/`) and `intake/` (Collect, Curate, Ingest)

- **Fetchers (`recipe/tools/`):** Pre-ingest scripts that download or import files into `intake/<source>/` and write `intake/manifests/<source>.json` (URL, timestamp, SHA-256, status). Optional API keys live in a gitignored `.env` (documented in `.env.example`) read only by the fetcher. Stages never import `recipe/tools/` or read `intake/` directly. Build fetchers on `commissure.patterns.fetch`, which supplies the fetch, save, and record rules: polite per-host spacing; a stop (exit 3, resumable) at the first bot challenge, never posing as a browser; PDF or HTML only, never overwriting different bytes; each page's own PDFs, else the page as HTML; and `import_saved` for documents a person saves by hand when a site refuses scripts. A solution's fetcher lists only its sources.
- **Collect -> Curate -> Ingest:**
  1. **Collect** raw files into one subfolder per source (`intake/<source>/`).
  2. **Curate** before first ingest and after each refresh: an SME checks `intake/manifests/<source>.json` and moves out-of-scope or duplicate files to `intake/_excluded/<source>/`, logging each in `intake/_excluded/CURATION.md` (`YYYY-MM-DD  <source>/<file>  excluded: <reason>  (initials)`). Moving rather than deleting keeps the manifest entry so fetchers do not re-download excluded files.
  3. **Ingest** each curated subfolder separately (`uv run commissure run --step ingest --source intake/<source> --role <role>`), never `intake/` as a whole. Re-ingesting after moving a file out records a tombstone so outputs drop it.
  4. **Document** collection, manifest checks, curation, and processing for SMEs in `docs/user-guide.md` (scaffolded by `commissure init` and shipped automatically by `commissure release build`).

## 3. Roles

Every `--step ingest` stamps a recipe-defined `--role <role>` (default `corpus`) on the folder's items: for example, `policy_document` (authorities), `clearance_package` (draft under review), and `context_report` (background reading). Stages set `Stage(..., roles=("...",))` so each step processes only the roles intended for it.
