"""Test recipe for the runner. Not a real recipe: it exists to exercise core."""

from commissure.api import Check, Stage

from .cite import carry, cite, one_group
from .extract import extract
from .schema import Carried, Cited, Entry, Score, Total
from .score import score
from .summarize import by_initial, require_no_quarantine, total

STAGES = [
    Stage(
        "extract",
        extract,
        schema=Entry,
        text="default",
        params={"delay": 0},
        checks=(
            Check("/amount", "Is this the amount the item states?"),
            Check("/name", "Is this the item's name?"),
            Check("/note", "Matches no payload field, on purpose (tests)."),
        ),
    ),
    Stage("score", score, schema=Score, needs=("extract",)),
    Stage(
        "by_initial", total, schema=Total, subject="group", needs=("extract",), group_by=by_initial
    ),
    Stage("total", total, schema=Total, subject="run", needs=("extract",)),
    # The same extraction under a gate that rejects any quarantine.
    Stage(
        "strict_extract",
        extract,
        schema=Entry,
        text="default",
        params={"delay": 0},
        gate=require_no_quarantine,
    ),
    # Evidence: core checks and resolves the pointers these stages put in their payloads.
    Stage(
        "cite",
        cite,
        schema=Cited,
        text=("ocr", "default"),
        params={"needle": "", "from": 0, "mode": "span", "page": 1},
        checks=(Check("/evidence/*", "Does the item's text say this, at this place?"),),
    ),
    Stage(
        "carry",
        carry,
        schema=Carried,
        subject="group",
        needs=("cite",),
        group_by=one_group,
        params={"mode": "copy"},
    ),
]
