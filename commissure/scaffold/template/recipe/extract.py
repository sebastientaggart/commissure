"""Extraction for this recipe.

Placeholder. An item stage: `def extract(ctx, item)` reads `item.text`, `item.structure`,
`item.source`, and any declared `item.upstream` / `item.upstream_outcomes`, returning a
`schema.py` model or a `RecordEnvelope` (`.warning`, `.quarantined`, `.skipped`) with a
non-empty reason code. A field that cannot be extracted is a warning or a quarantine,
never a plausible default. An exception you do not catch fails the run; when using
`ctx.llm`, catch `ExtractionFailed` (`model_output_invalid`) and `InputTooLarge`
(`input_too_large`) to return `RecordEnvelope.quarantined(...)`.
"""
