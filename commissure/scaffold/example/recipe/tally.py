"""Run stage: totals over every extracted comment, naming each one set aside and why."""

from collections import Counter

from commissure.api import Group, RunContext

from .schema import Tally


def tally(ctx: RunContext, group: Group) -> Tally:
    comments = [item.upstream["extract"] for item in group.items]
    positions = Counter(c.position for c in comments)
    topics = Counter(m.topic for c in comments for m in c.topics)
    return Tally(
        comments=len(comments),
        support=positions["support"],
        oppose=positions["oppose"],
        no_position=positions[None],
        topics=dict(sorted(topics.items(), key=lambda kv: (-kv[1], kv[0]))),
        set_aside=sorted(e.locator for e in group.excluded),
        set_aside_reasons={e.locator: e.reason for e in group.excluded},
    )
