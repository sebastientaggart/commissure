"""Group and run stages over extract's payloads."""

from .schema import Total


def by_initial(item):
    return item.upstream["extract"].name[0]


def total(ctx, group):
    amounts = ctx.map(lambda i: i.upstream["extract"].amount, group.items)
    return Total(
        key=group.key,
        count=len(amounts),
        total=sum(amounts),
        excluded=sorted(f"{e.locator}:{e.reason}" for e in group.excluded),
    )


def require_no_quarantine(counts):
    return [f"{counts['quarantined']} quarantined"] if counts["quarantined"] else []
