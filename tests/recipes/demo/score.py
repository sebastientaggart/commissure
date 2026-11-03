"""Item stage that needs extract."""

from .schema import Score


def score(ctx, item):
    entry = item.upstream["extract"]
    return Score(name=entry.name, doubled=entry.amount * 2)
