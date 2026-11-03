"""Analysis for this recipe.

Placeholder. Cross-item logic: group stages (`def consolidate(ctx, group)`, receiving
`group.items` for one group key) and run stages (`subject="run"`, one `Group` over all
complete items). Incomplete items land in `group.excluded` when `group_by` can place them
from the locator (or other fields always present on the item). A `group_by` that reads
upstream payloads cannot place incomplete items (they have none) and those stay unplaced.
Stages return a `schema.py` model or `RecordEnvelope`; they do not touch SQLite directly.
`ctx.map(fn, items)` runs per-item work on the run's bounded thread pool.
"""
