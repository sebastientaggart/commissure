"""Generate tests/fixtures/html_corpus/. Run: uv run python tests/fixtures/make_html_corpus.py

Two saved web pages, each written so the HTML parser's rules have one known answer:

- `Program Instruction PI-00-01 _ Example Agency.html`: a federal-site issuance page, under the
  name a browser gives a saved page (Firefox: the page title, `|` as ` _ `, spaces kept).
  Banner, header, navigation, search form, footer, and scripts around a `<main>` that holds
  the issuance: headings, a numbered field list, paragraphs with inline markup and `<br>`, a
  nested list, a table with `colspan`, and a hidden element. UTF-8, declared.
- `legacy.htm`: an older page with no `<main>` or `<article>`, saved in Windows-1252 without
  declaring it (curly quotes and an em dash in that encoding), with page header and footer
  around the body text. Expected: `content_root_body` and `encoding_fallback`.

The outputs are checked in; rerun this only to change the corpus, and review the diff.
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).parent / "html_corpus"
SAVED_PAGE = "Program Instruction PI-00-01 _ Example Agency.html"

POLICY = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Program Instruction PI-00-01 | Example Agency</title>
  <meta name="description" content="Reporting instructions for grant recipients.">
  <link rel="canonical" href="https://agency.example.gov/policy/pi-00-01">
  <script>window.analytics = { track: function () {} };</script>
  <style>.usa-banner { font-size: 12px; }</style>
</head>
<body>
  <a class="skip-link" href="#main-content">Skip to main content</a>
  <section class="usa-banner">An official website of the United States government</section>
  <header class="usa-header">
    <div class="logo">Example Agency</div>
    <form role="search"><label>Search</label><input name="q"><button>Go</button></form>
  </header>
  <nav class="usa-nav"><a href="/">Home</a> <a href="/policy">Policy</a></nav>
  <main id="main-content">
    <h1>Program Instruction PI-00-01</h1>
    <ol>
      <li>Log Number: PI-00-01</li>
      <li>Issuance Date: 01/15/2000</li>
      <li>Key Words: Reporting; Grants</li>
    </ol>
    <h2>Instruction</h2>
    <p>Grant recipients <strong>must</strong> submit the annual report
       by <em>March&nbsp;1</em>.</p>
    <p>Questions go to:<br>Office of Programs<br>Example Agency</p>
    <ul>
      <li>Financial data
        <ul><li>Quarterly totals</li><li>Annual totals</li></ul>
      </li>
      <li>Enrollment data</li>
    </ul>
    <h3>Funding table</h3>
    <table>
      <thead><tr><th>Program</th><th colspan="2">Amount (FY / total)</th></tr></thead>
      <tbody>
        <tr><td>Preschool</td><td>2000</td><td>$1,000</td></tr>
        <tr><td>Infant</td><td>2000</td><td>$500</td></tr>
      </tbody>
    </table>
    <div hidden>Draft note: do not publish.</div>
    <p>Sincerely,<br>Program Director</p>
  </main>
  <footer class="usa-footer">Example Agency &middot; Privacy &middot; Accessibility</footer>
  <script src="/js/site.js"></script>
</body>
</html>
"""

LEGACY = (
    "<html><head><title>Annual Report 1999</title></head><body>\n"
    "<header>Agency Home | Reports | Contact</header>\n"
    "<h1>Annual Report 1999</h1>\n"
    "<p>The program served 1,200 children — a “record” year.</p>\n"
    "<p>See the appendix for county totals.</p>\n"
    "<footer>Last updated 1999</footer>\n"
    "</body></html>\n"
)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / SAVED_PAGE).write_text(POLICY, encoding="utf-8")
    (OUT / "legacy.htm").write_bytes(LEGACY.encode("cp1252"))


if __name__ == "__main__":
    main()
