"""Item stage: read name and amount from each JSON item's parsed text."""

import json
import time

from commissure.api import Diagnostic, RecordEnvelope

from .schema import Entry


def extract(ctx, item):
    time.sleep(ctx.params["delay"])
    data = json.loads(item.text)
    name = data["name"]
    if name.startswith("skip"):
        return RecordEnvelope.skipped("not_applicable", Diagnostic(code="not_applicable"))
    if name == "boom":
        raise RuntimeError("an unhandled bug in recipe code")
    if name == "badtype":
        return {"name": name, "amount": "not a number"}  # schema violation
    if data["amount"] < 0:
        return RecordEnvelope.quarantined(
            "negative_amount", Diagnostic(code="negative_amount", field="amount")
        )
    entry = Entry(name=name, amount=data["amount"])
    if data["amount"] == 0:
        return RecordEnvelope.warning(entry, "zero_amount")
    return entry
