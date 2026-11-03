"""The single import surface for recipes.

Recipes import from this module (and optionally `commissure.patterns`), never from
other core modules or provider SDKs.

    from commissure.api import Stage, RecordEnvelope, Diagnostic

    STAGES = [Stage("extract", extract, schema=Report, text="default")]

A stage function takes `(ctx, subject)`: an `Item` for item stages, a `Group` for
group and run stages. It returns a payload (an instance of its schema) or a
`RecordEnvelope` (`.valid`, `.warning`, `.quarantined`, `.skipped`). An exception it
does not handle fails the run: expected bad data is a quarantine, not a crash.

A payload points at what it rests on with `Evidence.span`, `.on_page`, or
`.whole_item`, built from an `Item` the stage received; core checks it and fills in
locator, page, and OCR trust when the record is stored (`commissure/evidence.py`).
"""

from commissure.chunk import (
    PreparedSource,
    TextChunk,
    VerbatimMatch,
    chunk_item,
    chunk_text,
    find_verbatim_span,
    pack_batches,
    prepare_source,
    reduce_tree,
    renumber_chunks,
)
from commissure.deliver.html import (
    count_external_resource_urls,
    page_external_resource_count,
    render_page,
)
from commissure.evidence import Evidence, EvidenceError
from commissure.limits import Limits
from commissure.llm import (
    Completion,
    Extraction,
    ExtractionFailed,
    InputTooLarge,
    LLMConfigError,
    LLMError,
    LLMTransportError,
    MissingAPIKeyError,
    Part,
    RemoteModelRefused,
    SystematicModelFailure,
    estimate_tokens,
)
from commissure.recipes import (
    Excluded,
    Group,
    Interface,
    Item,
    Source,
    Stage,
    UpstreamOutcome,
)
from commissure.records import OUTCOMES, Diagnostic, RecordEnvelope
from commissure.review import Check
from commissure.runner import RunContext
from commissure.search import Hit, SearchFromWorker, SearchUsageError
from commissure.serve import (
    ActionContext,
    ActionError,
    BusyStatus,
    InterfaceView,
    RecordInfo,
    SourceInfo,
    StepRunSummary,
    StepStatus,
)

__all__ = [
    "OUTCOMES",
    "ActionContext",
    "ActionError",
    "BusyStatus",
    "Check",
    "Completion",
    "Diagnostic",
    "Evidence",
    "EvidenceError",
    "Excluded",
    "Extraction",
    "ExtractionFailed",
    "Group",
    "Hit",
    "InputTooLarge",
    "Interface",
    "InterfaceView",
    "Item",
    "LLMConfigError",
    "LLMError",
    "LLMTransportError",
    "Limits",
    "MissingAPIKeyError",
    "Part",
    "PreparedSource",
    "RecordEnvelope",
    "RecordInfo",
    "RemoteModelRefused",
    "RunContext",
    "SearchFromWorker",
    "SearchUsageError",
    "Source",
    "SourceInfo",
    "Stage",
    "StepRunSummary",
    "StepStatus",
    "SystematicModelFailure",
    "TextChunk",
    "UpstreamOutcome",
    "VerbatimMatch",
    "chunk_item",
    "chunk_text",
    "count_external_resource_urls",
    "estimate_tokens",
    "find_verbatim_span",
    "pack_batches",
    "page_external_resource_count",
    "prepare_source",
    "reduce_tree",
    "render_page",
    "renumber_chunks",
]
