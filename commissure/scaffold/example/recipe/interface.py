"""`commissure serve`: the tally, and every comment with the words its result rests on.

A body fragment built from core's `report.css` primitives (`.kpis`, `.chart-bars`, badges);
core supplies the page shell, the pipeline steps, and `/review` for grading samples.
"""

from html import escape

from commissure.api import Interface, InterfaceView

BADGE = {"support": "badge-ok", "oppose": "badge-err", None: "badge-warn"}


def _quote(ev) -> str:
    page = f", page {ev.page}" if ev.page else ""
    where = f'<small class="muted">{escape(ev.locator or "")}{page}</small>'
    return f"&ldquo;{escape(ev.text or '')}&rdquo; {where}"


def render(view: InterfaceView) -> str:
    tallies = view.records("tally")
    if not tallies:
        return (
            "<section><h2>No results yet</h2><p>Run the pipeline from the steps drawer, "
            "or from a terminal (see README.md):</p><pre>"
            "uv run commissure run --step ingest --source intake/comments --role comment\n"
            "uv run commissure run --step parse\n"
            "uv run commissure run --step extract\n"
            "uv run commissure run --step tally</pre></section>"
        )
    t = tallies[0].payload
    kpis = "".join(
        f'<div class="kpi"><strong>{n}</strong>{escape(label)}</div>'
        for n, label in (
            (t.comments, "Comments read"),
            (t.support, "Support"),
            (t.oppose, "Oppose"),
            (t.no_position, "No stated position"),
            (len(t.set_aside), "Set aside for a person"),
        )
    )
    top = max(t.topics.values(), default=1)
    bars = "".join(
        f'<li><span class="chart-bar-label">{escape(topic)}</span>'
        f'<span class="chart-bar-track"><span class="chart-bar" '
        f'style="width: {100 * n / top:.0f}%"></span></span>'
        f'<span class="chart-bar-value">{n}</span></li>'
        for topic, n in t.topics.items()
    )
    rows = []
    for rec in sorted(view.records("extract"), key=lambda r: r.subject_key):
        c = rec.payload
        quote = _quote(c.position_evidence) if c.position_evidence else "<em>none found</em>"
        topics = "<br>".join(
            f"<strong>{escape(m.topic)}</strong>: {_quote(m.evidence)}" for m in c.topics
        )
        rows.append(
            f'<tr><td><span class="badge {BADGE[c.position]}">{escape(c.position or "none")}'
            f"</span></td><td>{quote}</td><td>{topics}</td></tr>"
        )
    set_aside = "".join(
        f"<li>{escape(loc)}: {escape(t.set_aside_reasons[loc])}</li>" for loc in t.set_aside
    )
    return (
        f'<section><h2>Proposal 7 comments</h2><div class="kpis">{kpis}</div></section>'
        f'<section><h2>Topics raised</h2><ul class="chart-bars">{bars}</ul></section>'
        f"<section><h2>Set aside</h2><ul>{set_aside or '<li>None</li>'}</ul></section>"
        "<section><h2>Every comment, with its evidence</h2><p>Grade a random sample against "
        'the originals at <a href="/review">/review</a>.</p><table data-sortable><thead><tr>'
        "<th>Position</th><th>Stated as</th><th>Topics</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
    )


INTERFACE = Interface(
    title="Proposal 7 public comments",
    subtitle="A Commissure example: fictional comments, deterministic extraction, cited results.",
    render=render,
)
