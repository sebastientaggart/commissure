"""Release and repository lifecycle tooling (`commissure/tools/`).

What belongs here:
- Developer and operator tooling that packages, audits, or manages solution and release trees
  outside the runtime pipeline (for example, `commissure/tools/release.py`).

What does not belong here:
- Core runtime substrate touching `state.db`, `blobs/`, parsers, `ctx.llm`, or stage execution
  (lives in `commissure/*.py`).
- Reusable extraction, matching, or deliverable helpers imported by recipes (lives in a recipe
  first, then graduates to `commissure/patterns/` under the Rule of Two).
- Domain- or source-specific pre-ingest fetchers and scripts that populate `intake/` or read a
  solution's `.env` (lives in `solutions/<name>/recipe/tools/`).

Modules in `commissure/tools/` are never imported by recipes (`commissure/api.py`) or by runtime
core modules, and never name a client, corpus, or business document type.
"""
