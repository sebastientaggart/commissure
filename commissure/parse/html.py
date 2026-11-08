"""HTML parser: the page's main content as headings, paragraphs, list items, and tables.

A saved web page is mostly chrome: banners, navigation, footers, scripts. The text is read
from one content root: the page's `<main>`, else its only `<article>`, else `<body>` (with
issue `content_root_body`, because chrome may then be in the text). Inside the root, these
subtrees are dropped: script, style, template, noscript, svg, iframe, form, button, select,
nav, aside, anything `hidden` or `aria-hidden="true"`, and, when the root is `<body>`, its
header and footer. Each drop is counted in `metadata.dropped`, so the structure says what
was left out.

Blocks follow the DOCX parser: headings (h1 to h6) keep a heading path; paragraphs, list
items, definition terms, quotes, captions, preformatted text, and runs of text directly in
a container are paragraphs; tables are tab-separated rows (a `colspan` repeats as None
cells, so rows stay rectangular). Whitespace is collapsed except in `<pre>`; `<br>` is a
line break. Nothing from attributes (links, alt text) enters the text.

Encoding: a byte-order mark, then a declared charset in the first 4 KB, then UTF-8. Bytes
that do not decode under that encoding are decoded as Windows-1252 instead, with issue
`encoding_fallback`; they are never dropped or replaced silently.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from commissure.limits import DEFAULT_LIMITS, LimitExceeded, Limits, check_file_bytes
from commissure.parse.model import DocBuilder, ParsedDoc, rows_to_text

VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
DROP = frozenset(
    [
        "script",
        "style",
        "template",
        "noscript",
        "svg",
        "math",
        "iframe",
        "object",
        "canvas",
        "form",
        "button",
        "select",
        "textarea",
        "nav",
        "aside",
        "dialog",
    ]
)
CHROME = frozenset({"header", "footer"})  # dropped only when the root is <body>
HEADINGS = {f"h{n}": n for n in range(1, 7)}
# Elements whose content is one paragraph; a new one (or any block) closes an open <p>.
PARAGRAPHS = frozenset(
    ["p", "li", "dt", "dd", "blockquote", "figcaption", "caption", "address", "pre", "summary"]
)
BLOCKS = (
    frozenset(
        [
            "div",
            "section",
            "article",
            "main",
            "header",
            "footer",
            "ul",
            "ol",
            "dl",
            "table",
            "tr",
            "td",
            "th",
            "thead",
            "tbody",
            "tfoot",
            "figure",
            "details",
            "fieldset",
            "hr",
            "body",
            "html",
        ]
    )
    | PARAGRAPHS
    | frozenset(HEADINGS)
)
# An open element of these kinds is closed by a new sibling of a kind in the set.
SIBLING_CLOSES = {
    "p": frozenset({"p"}) | BLOCKS,
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "tr": frozenset({"tr"}),
    "td": frozenset({"td", "th", "tr"}),
    "th": frozenset({"td", "th", "tr"}),
    "option": frozenset({"option"}),
}
CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)
# Source whitespace, including line breaks and no-break spaces, is one space: only `<br>`
# breaks a line (outside `<pre>`, which keeps its text as written).
WS_RE = re.compile(r"[ \t\r\n\f\v\u00a0]+")
LINE_RE = re.compile(r"[ \t\f\v\u00a0]+")


# --- a small tolerant tree ---------------------------------------------------------------


@dataclass
class Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = None

    def find_all(self, tag: str) -> list[Node]:
        out: list[Node] = []
        for c in self.children:
            if isinstance(c, Node):
                if c.tag == tag:
                    out.append(c)
                out += c.find_all(tag)
        return out

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)


class _TreeBuilder(HTMLParser):
    """HTML into `Node`s, closing elements the way browsers imply (`<p>`, `<li>`, cells)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        # A block start closes open elements that cannot contain it (an open <p> before a
        # <div>, the previous <li> before an <li>), up to the nearest container.
        while len(self.stack) > 1:
            closes = SIBLING_CLOSES.get(self.stack[-1].tag)
            if closes is None or tag not in closes:
                break
            self.stack.pop()
        node = Node(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID and self.stack[-1].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        # Close up to the matching open element; a stray end tag closes nothing.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


# --- decoding ----------------------------------------------------------------------------


def _decode(raw: bytes) -> tuple[str, str, bool]:
    """`(text, encoding, fell_back)`: BOM, then declared charset, then UTF-8."""
    for bom, enc in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                     (codecs.BOM_UTF16_BE, "utf-16-be")):  # fmt: skip
        if raw.startswith(bom):
            return raw[len(bom) :].decode(enc, errors="replace"), enc, False
    declared = CHARSET_RE.search(raw[:4096])
    enc = "utf-8"
    if declared:
        try:
            enc = codecs.lookup(declared.group(1).decode("ascii")).name
        except LookupError:
            enc = "utf-8"
    try:
        return raw.decode(enc), enc, False
    except UnicodeDecodeError:
        # Windows-1252 decodes every byte but five; those five are replaced and counted.
        return raw.decode("cp1252", errors="replace"), "cp1252", True


# --- content root and blocks -------------------------------------------------------------


def _hidden(node: Node) -> bool:
    return "hidden" in node.attrs or (node.attrs.get("aria-hidden") or "").lower() == "true"


def _content_root(doc: Node) -> tuple[Node, str]:
    mains = [m for m in doc.find_all("main") if not _hidden(m)]
    if mains:
        return mains[0], "main"
    articles = [a for a in doc.find_all("article") if not _hidden(a)]
    if len(articles) == 1:
        return articles[0], "article"
    bodies = doc.find_all("body")
    return (bodies[0], "body") if bodies else (doc, "body")


def _clean(text: str) -> str:
    """Collapse whitespace within each line; keep the line breaks `<br>` made."""
    lines = [LINE_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


@dataclass
class _Walker:
    b: DocBuilder
    root_kind: str
    limits: Limits
    dropped: dict[str, int] = field(default_factory=dict)
    path: list[tuple[int, str]] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)
    table_rows: int = 0
    table_cells: int = 0

    def headings(self) -> list[str]:
        return [t for _, t in self.path]

    def flush(self) -> None:
        text = _clean("".join(self.buffer))
        self.buffer.clear()
        if text:
            self.b.block("paragraph", text, heading_path=self.headings())

    def skip(self, node: Node) -> bool:
        reason = None
        if node.tag in DROP or node.tag in CHROME and self.root_kind == "body":
            reason = node.tag
        elif _hidden(node):
            reason = "hidden"
        if reason:
            self.dropped[reason] = self.dropped.get(reason, 0) + 1
        return reason is not None

    def inline(self, node: Node, pre: bool = False) -> str:
        """The text of an element read as one run: `<br>` is a line break, drops are dropped."""
        parts: list[str] = []
        for c in node.children:
            if isinstance(c, str):
                parts.append(c if pre else WS_RE.sub(" ", c))
            elif c.tag == "br":
                parts.append("\n")
            elif not self.skip(c):
                text = self.inline(c, pre)
                parts.append(f" {text} " if c.tag in BLOCKS and not pre else text)
        return "".join(parts)

    def walk(self, node: Node) -> None:
        for c in node.children:
            if isinstance(c, str):
                self.buffer.append(WS_RE.sub(" ", c))
                continue
            if self.skip(c):
                continue
            if c.tag == "br":
                self.buffer.append("\n")
            elif c.tag in HEADINGS:
                self.flush()
                text = _clean(self.inline(c)).replace("\n", " ")
                if text:
                    level = HEADINGS[c.tag]
                    while self.path and self.path[-1][0] >= level:
                        self.path.pop()
                    self.b.block("heading", text, level=level, heading_path=self.headings())
                    self.path.append((level, text))
            elif c.tag == "table":
                self.flush()
                self.table(c)
            elif c.tag == "pre":
                self.flush()
                text = self.inline(c, pre=True).strip("\n")
                if text.strip():
                    self.b.block("paragraph", text, heading_path=self.headings())
            elif c.tag in PARAGRAPHS:
                self.flush()
                if any(isinstance(g, Node) and g.tag in BLOCKS for g in c.children):
                    self.walk(c)  # a list item holding a nested list, a quote of paragraphs
                    self.flush()
                else:
                    self.buffer.append(self.inline(c))
                    self.flush()
            elif c.tag in BLOCKS:
                self.flush()
                self.walk(c)
                self.flush()
            else:
                self.walk(c)  # inline element: its text joins the current paragraph

    def table(self, node: Node) -> None:
        rows: list[list[str | None]] = []
        for tr in node.find_all("tr"):
            if _nearest_table(tr) is not node:
                continue  # a nested table's rows belong to its own cell
            row: list[str | None] = []
            for cell in tr.children:
                if isinstance(cell, Node) and cell.tag in ("td", "th") and not self.skip(cell):
                    row.append(_clean(self.inline(cell)).replace("\n", " "))
                    span = cell.attrs.get("colspan") or "1"
                    row += [None] * (max(1, int(span)) - 1 if span.isdigit() else 0)
            if row:
                rows.append(row)
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [None] * (width - len(r)) for r in rows]
        self.table_rows += len(rows)
        self.table_cells += len(rows) * width
        for code, actual, limit in (
            ("too_many_rows", self.table_rows, self.limits.max_table_rows),
            ("too_many_cells", self.table_cells, self.limits.max_table_cells),
        ):
            if actual > limit:
                what = code.removeprefix("too_many_")
                raise LimitExceeded(
                    code,
                    f"HTML tables exceed max_table_{what} ({limit})",
                    actual=actual,
                    limit=limit,
                )
        index = self.b.table(rows, heading_path=self.headings())
        self.b.block("table", rows_to_text(rows), table=index, heading_path=self.headings())


def _nearest_table(node: Node) -> Node | None:
    p = node.parent
    while p is not None and p.tag != "table":
        p = p.parent
    return p


def _meta(doc: Node) -> dict[str, str]:
    out: dict[str, str] = {}
    titles = doc.find_all("title")
    if titles and (t := _clean(titles[0].text()).replace("\n", " ")):
        out["title"] = t
    for m in doc.find_all("meta"):
        name = (m.attrs.get("name") or m.attrs.get("property") or "").lower()
        value = (m.attrs.get("content") or "").strip()
        if value and name in ("description", "og:title", "article:published_time", "dcterms.date"):
            out[name] = value
    for link in doc.find_all("link"):
        if (link.attrs.get("rel") or "").lower() == "canonical" and link.attrs.get("href"):
            out["canonical"] = link.attrs["href"] or ""
    html_nodes = doc.find_all("html")
    if html_nodes and html_nodes[0].attrs.get("lang"):
        out["lang"] = html_nodes[0].attrs["lang"] or ""
    return out


def parse_html(path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
    check_file_bytes(path, limits)
    text, encoding, fell_back = _decode(path.read_bytes())
    tree = _TreeBuilder()
    tree.feed(text)
    tree.close()
    root, kind = _content_root(tree.root)
    b = DocBuilder()
    walker = _Walker(b, kind, limits)
    walker.walk(root)
    walker.flush()
    b.metadata = {**_meta(tree.root), "encoding": encoding, "content_root": kind,
                  "dropped": dict(sorted(walker.dropped.items()))}  # fmt: skip
    if kind == "body":
        b.issue(
            "content_root_body", detail="no <main> or single <article>; chrome may be in the text"
        )
    if fell_back:
        b.issue(
            "encoding_fallback", detail="bytes did not decode as declared; read as Windows-1252"
        )
    return b.build()
