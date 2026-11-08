"""HTML parser: content root, dropped chrome, block structure, tables, decoding.

Each rule runs on its input and on a sibling it must leave alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from commissure.limits import LimitExceeded, Limits
from commissure.parse import DEFAULT_FOR, PARSERS
from commissure.parse.html import parse_html
from commissure.parse.step import judge_doc


def page(tmp_path: Path, body: str, *, head: str = "", name: str = "p.html",
         encoding: str = "utf-8", raw: bytes | None = None) -> Path:  # fmt: skip
    path = tmp_path / name
    doc = f"<!doctype html><html lang='en'><head>{head}</head><body>{body}</body></html>"
    path.write_bytes(raw if raw is not None else doc.encode(encoding))
    return path


def blocks(doc) -> list[tuple[str, str]]:
    return [(b.kind, doc.text[b.start : b.end]) for b in doc.blocks]


CHROME_PAGE = """
<header><div class="usa-banner">An official website of the United States government</div></header>
<nav><a href="/">Home</a> <a href="/policy">Policy</a></nav>
<main>
  <h1>Final Rule to Delay</h1>
  <p>Log Number: ACF-OHS-PI-24-07&nbsp;&nbsp;|&nbsp;&nbsp;Issuance Date: 11/06/2024</p>
  <script>var tracking = "ignore me";</script><style>p { color: red }</style>
  <h2>Instruction</h2>
  <p>On November 6, 2024, ACF <strong>published</strong> a final rule
     in the Federal Register.</p>
  <ul><li>First point</li><li>Second point<ul><li>Nested point</li></ul></li></ul>
  <p>U.S. Department<br>of Health and Human Services</p>
  <div hidden>Hidden text</div><span aria-hidden="true">Icon text</span>
  <pre>  keep   spacing
line two</pre>
  <aside>Related links</aside>
</main>
<footer>Footer &copy; HHS</footer>
"""


def test_main_is_the_content_and_chrome_is_dropped_and_counted(tmp_path):
    doc = parse_html(page(tmp_path, CHROME_PAGE, head="<title>PI-24-07 | Site</title>"))
    assert doc.metadata["content_root"] == "main"
    assert doc.metadata["title"] == "PI-24-07 | Site" and doc.metadata["lang"] == "en"
    dropped = doc.metadata["dropped"]
    assert (dropped["script"], dropped["style"], dropped["aside"], dropped["hidden"]) == (
        1,
        1,
        1,
        2,
    )
    for gone in ("official website", "Home", "ignore me", "color: red", "Hidden text",
                 "Icon text", "Related links", "Footer"):  # fmt: skip
        assert gone not in doc.text, gone
    assert blocks(doc) == [
        ("heading", "Final Rule to Delay"),
        ("paragraph", "Log Number: ACF-OHS-PI-24-07 | Issuance Date: 11/06/2024"),
        ("heading", "Instruction"),
        ("paragraph", "On November 6, 2024, ACF published a final rule in the Federal Register."),
        ("paragraph", "First point"),
        ("paragraph", "Second point"),
        ("paragraph", "Nested point"),
        ("paragraph", "U.S. Department\nof Health and Human Services"),
        ("paragraph", "  keep   spacing\nline two"),
    ]
    by_text = {doc.text[b.start : b.end]: b for b in doc.blocks}
    assert by_text["Instruction"].heading_path == ["Final Rule to Delay"]
    assert by_text["Nested point"].heading_path == ["Final Rule to Delay", "Instruction"]
    assert judge_doc(doc).outcome == "valid"


def test_without_main_a_single_article_is_the_root_and_otherwise_body_is_flagged(tmp_path):
    body = "<header>Site</header><article><header><h1>Kept</h1></header><p>Text</p></article>"
    one = parse_html(page(tmp_path, body, name="a.html"))
    assert one.metadata["content_root"] == "article"
    assert blocks(one) == [("heading", "Kept"), ("paragraph", "Text")]  # its own header stays
    assert judge_doc(one).outcome == "valid"

    two = parse_html(page(tmp_path, "<header>Site banner</header><article><p>One</p></article>"
                                    "<article><p>Two</p></article><footer>Foot</footer>",
                          name="b.html"))  # fmt: skip
    assert two.metadata["content_root"] == "body"
    assert [i["code"] for i in two.issues] == ["content_root_body"]
    assert "Site banner" not in two.text and "Foot" not in two.text  # body chrome dropped
    assert blocks(two) == [("paragraph", "One"), ("paragraph", "Two")]
    env = judge_doc(two)
    assert (env.outcome, env.reason_code) == ("warning", "content_root_body")


def test_unclosed_paragraphs_list_items_and_cells_close_the_way_browsers_close_them(tmp_path):
    doc = parse_html(page(tmp_path, "<main><p>one<p>two<div>three</div><ul><li>a<li>b</ul>"
                                    "<table><tr><td>x<td>y<tr><td>z</table></main>"))  # fmt: skip
    assert blocks(doc)[:5] == [("paragraph", "one"), ("paragraph", "two"), ("paragraph", "three"),
                               ("paragraph", "a"), ("paragraph", "b")]  # fmt: skip
    assert doc.tables[0]["rows"] == [["x", "y"], ["z", None]]


def test_tables_are_rectangular_tab_separated_and_keep_nested_tables_in_their_cell(tmp_path):
    doc = parse_html(
        page(
            tmp_path,
            """<main><h2>Costs</h2><table>
      <thead><tr><th colspan="2">Program</th><th>Cost</th></tr></thead>
      <tbody><tr><td>Program A</td><td>Preschool</td><td>$1&nbsp;000</td></tr>
      <tr><td>Program B</td><td><table><tr><td>inner</td></tr></table></td><td>2</td></tr></tbody>
    </table></main>""",
        )
    )
    [table] = doc.tables
    assert table["rows"] == [["Program", None, "Cost"], ["Program A", "Preschool", "$1 000"],
                             ["Program B", "inner", "2"]]  # fmt: skip
    assert table["heading_path"] == ["Costs"]
    [tb] = [b for b in doc.blocks if b.kind == "table"]
    assert (
        doc.text[tb.start : tb.end]
        == "Program\t\tCost\nProgram A\tPreschool\t$1 000\nProgram B\tinner\t2"
    )


def test_table_limits_are_enforced(tmp_path):
    rows = "".join(f"<tr><td>{i}</td></tr>" for i in range(5))
    path = page(tmp_path, f"<main><table>{rows}</table></main>")
    with pytest.raises(LimitExceeded) as err:
        parse_html(path, Limits(max_table_rows=4))
    assert err.value.code == "too_many_rows"
    assert len(parse_html(path, Limits(max_table_rows=5)).tables[0]["rows"]) == 5


def test_decoding_follows_bom_then_declared_charset_then_utf8_and_flags_a_fallback(tmp_path):
    head = '<meta charset="windows-1252">'
    declared = page(
        tmp_path, "<main><p>café — “quoted”</p></main>", head=head, encoding="cp1252", name="d.html"
    )
    doc = parse_html(declared)
    assert doc.metadata["encoding"] == "cp1252" and "café — “quoted”" in doc.text
    assert doc.issues == []

    undeclared = page(tmp_path, "<main><p>café</p></main>", encoding="cp1252", name="u.html")
    doc = parse_html(undeclared)
    assert doc.metadata["encoding"] == "cp1252" and "café" in doc.text
    assert [i["code"] for i in doc.issues] == ["encoding_fallback"]

    utf8 = page(tmp_path, "<main><p>café</p></main>", name="e.html")
    assert parse_html(utf8).issues == [] and "café" in parse_html(utf8).text

    bom = tmp_path / "bom.html"
    bom.write_bytes(b"\xef\xbb\xbf<main><p>caf\xc3\xa9</p></main>")
    assert parse_html(bom).text == "café"


def test_a_page_with_no_content_has_no_readable_text(tmp_path):
    doc = parse_html(page(tmp_path, "<nav>Only navigation</nav><main><script>x()</script></main>"))
    assert doc.text == ""
    assert judge_doc(doc).outcome == "quarantined"


def test_html_and_htm_parse_by_default():
    assert DEFAULT_FOR[".html"] == DEFAULT_FOR[".htm"] == "html"
    assert PARSERS["html"].extensions == {".html", ".htm"}


def test_source_line_breaks_are_spaces_in_containers_and_in_paragraphs(tmp_path):
    doc = parse_html(page(tmp_path, "<main><div>Text directly\n   in a container</div>"
                                    "<p>A paragraph\n    over two lines</p></main>"))  # fmt: skip
    assert blocks(doc) == [
        ("paragraph", "Text directly in a container"),
        ("paragraph", "A paragraph over two lines"),
    ]
