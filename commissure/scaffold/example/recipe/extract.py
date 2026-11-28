"""Item stage: the commenter's position and the topics they raise, as exact quotes.

Deterministic on purpose: a fixed phrase list (recipe/AGENTS.md, Domain rules) beats a model
for a form this regular, and every result is a span core checks against the text.
"""

import re

from commissure.api import Diagnostic, Evidence, Item, RecordEnvelope, RunContext

from .schema import Comment, Mention

HEADER = "Public comment on Proposal 7: evening and Sunday hours at the Alder Creek Library"
PHRASES = {
    "support": re.compile(r"\b(I support|I am in favor|Please approve)\b", re.IGNORECASE),
    "oppose": re.compile(r"\b(I oppose|I am against|Please reject)\b", re.IGNORECASE),
}
TOPICS = {
    "evenings": r"\b(evenings?|night)",
    "sundays": r"\b(sundays?|weekends?)\b",
    "cost": r"\b(levy|tax|cost)",
    "staffing": r"\bstaff",
    "students": r"\b(students?|homework)\b",
    "parking": r"\bparking\b",
}


def extract(ctx: RunContext, item: Item) -> Comment | RecordEnvelope[Comment]:
    text = item.text or ""
    header = text.find(HEADER)
    if header < 0:
        return RecordEnvelope.quarantined(
            "missing_form_header",
            Diagnostic(code="missing_form_header", message="Not on the comment form."),
        )
    body = header + len(HEADER)  # the form's own header names topics; never read from it

    found = {pos: [m for m in rx.finditer(text, body)] for pos, rx in PHRASES.items()}
    if found["support"] and found["oppose"]:
        return RecordEnvelope.quarantined(
            "conflicting_position",
            Diagnostic(
                code="conflicting_position",
                message="States both support and opposition; a person decides.",
            ),
        )
    topics = []
    for topic, pattern in TOPICS.items():
        m = re.compile(pattern, re.IGNORECASE).search(text, body)
        if m:
            topics.append(
                Mention(topic=topic, evidence=Evidence.span(item, *m.span(), kind="topic"))
            )
    for position, matches in found.items():
        if matches:
            first = matches[0]
            return Comment(
                position=position,
                position_evidence=Evidence.span(item, *first.span(), kind="position_phrase"),
                topics=topics,
            )
    return RecordEnvelope.warning(
        Comment(position=None, position_evidence=None, topics=topics),
        "no_position_phrase",
        Diagnostic(code="no_position_phrase", message="No support or oppose phrase."),
    )
