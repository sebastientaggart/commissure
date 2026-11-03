"""Evidence stages: point at `needle` (first found at or after `from`) in an item's text,
or break a pointer on purpose.

`mode` picks the pointer (`span`, `page`, `whole`) or one way a recipe can get it wrong;
core must refuse every wrong one when the record is stored.
"""

from commissure.api import Diagnostic, Evidence, RecordEnvelope

from .schema import Carried, Cited


def cite(ctx, item):
    needle, mode = ctx.params["needle"], ctx.params["mode"]
    start = item.text.find(needle, ctx.params["from"]) if needle and item.text else -1
    if start < 0:
        return RecordEnvelope.skipped("not_found", Diagnostic(code="not_found"))
    ev = Evidence.span(item, start, start + len(needle), kind="quote")
    broken = {
        "bad_slice": {"text": f"not {needle}"},
        "foreign": {"item_version_id": 10**6},
        "other_text": {"extracted_text_id": (item.extracted_text_id or 0) + 1000},
        "out_of_range": {"end": len(item.text) + 5},
        "recipe_page": {"page": 1},
        "recipe_confidence": {"confidence": 99.0},
        "forged": {"via_ocr": False, "locator": item.locator},
    }
    if mode == "page":
        ev = Evidence.on_page(item, ctx.params["page"], kind="page")
    elif mode == "whole":
        ev = Evidence.whole_item(item, kind="cleaned_text")
    elif mode in broken:
        ev = ev.model_copy(update=broken[mode])
    return Cited(quote=needle, evidence=[ev])


def one_group(item):
    return "all"


def carry(ctx, group):
    evidence = [e for item in group.items for e in item.upstream["cite"].evidence]
    if ctx.params["mode"] == "alter":
        evidence = [e.model_copy(update={"page": 99}) for e in evidence]
    return Carried(evidence=evidence)
