"""Offline page shell: brand fragments, the resource scan, and which files get copied."""

from __future__ import annotations

from pathlib import Path

import pytest

from commissure.api import (
    count_external_resource_urls,
    page_external_resource_count,
    render_page,
)

ANCHOR = '<a class="site-name" href="https://example.org/"><span>Acme</span></a>'
CDN = '<link rel="stylesheet" href="https://cdn.example.com/pico.min.css">'


def _page(tmp_path: Path, brand: Path | None, **kwargs) -> str:
    templates = tmp_path / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "body.html.j2").write_text("<p>{{ name }}</p>", encoding="utf-8")
    return render_page(
        template_dir=templates,
        template_name="body.html.j2",
        context={"name": "<em>kept</em>"},
        title="Title <x>",
        subtitle="Sub",
        brand_dir=brand,
        **kwargs,
    )


def test_brand_fragment_is_inserted_unescaped_and_body_is_escaped(tmp_path: Path) -> None:
    brand = tmp_path / "brand"
    brand.mkdir()
    (brand / "footer.html").write_text("<p>Prepared <em>for</em> Acme</p>", encoding="utf-8")
    (brand / "brand.css").write_text(":root { --pico-primary: #0b4778; }\n", encoding="utf-8")
    (brand / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n', encoding="utf-8"
    )
    header = (
        '<a class="site-name" href="https://example.org/">'
        '<img src="static/brand/logo.svg" alt="">'
        "<span>Acme</span></a>"
    )
    (brand / "header.html").write_text(header, encoding="utf-8")

    out = tmp_path / "out"
    html = _page(tmp_path, brand, outputs_dir=out)

    assert "<span>Acme</span>" in html
    assert 'src="static/brand/logo.svg"' in html
    assert "<em>for</em>" in html
    assert "<em>kept</em>" not in html
    assert "&lt;em&gt;kept&lt;/em&gt;" in html
    assert "Title &lt;x&gt;" in html
    assert page_external_resource_count(html, brand) == 0
    assert (out / "static" / "brand" / "logo.svg").is_file()
    assert (out / "static" / "brand" / "brand.css").is_file()
    assert "static/brand/brand.css" in html
    assert (out / "static" / "report.js").is_file()
    assert (out / "static" / "report.css").is_file()
    assert (out / "static" / "pico.min.css").is_file()
    assert (out / "static" / "licenses" / "pico.LICENSE.md").is_file()
    assert not (out / "static" / "chart.umd.min.js").exists()
    assert not (out / "static" / "licenses" / "chartjs.LICENSE.md").exists()
    assert not (out / "static" / "brand" / "header.html").exists()


def test_omitted_brand_renders_shared_app_header_and_default_footer(tmp_path: Path) -> None:
    out = tmp_path / "out"
    html = _page(tmp_path, None, outputs_dir=out)
    assert '<header class="app-header">' in html
    assert '<h1 class="app-title">Title &lt;x&gt;</h1>' in html
    assert "app-brand-slot" not in html
    assert '<footer class="site-footer app-footer">' in html
    assert "&copy; Commissure" in html
    assert "brand.css" not in html
    assert "static/report.js" in html
    assert (out / "static" / "report.js").is_file()
    assert not (out / "static" / "brand").exists()

    nested = _page(tmp_path, None, static_prefix="../static")
    assert 'href="../static/report.css"' in nested
    assert 'src="../static/report.js"' in nested


def test_blank_header_file_adds_no_brand_slot(tmp_path: Path) -> None:
    brand = tmp_path / "brand"
    brand.mkdir()
    (brand / "header.html").write_text(" \n", encoding="utf-8")
    html = _page(tmp_path, brand)
    assert '<header class="app-header">' in html
    assert "app-brand-slot" not in html


def test_citation_anchor_is_allowed_and_a_cdn_stylesheet_is_not(tmp_path: Path) -> None:
    assert count_external_resource_urls(ANCHOR) == 0
    assert count_external_resource_urls(CDN) == 1
    assert count_external_resource_urls('<img src="https://cdn.example.com/a.png">') == 1
    assert count_external_resource_urls('background: url("https://cdn.example.com/a.png")') == 1

    brand = tmp_path / "brand"
    brand.mkdir()
    (brand / "header.html").write_text(ANCHOR, encoding="utf-8")
    out = tmp_path / "cited"
    html = _page(tmp_path, brand, outputs_dir=out)
    assert "https://example.org/" in html
    assert page_external_resource_count(html, brand) == 0
    assert (out / "static" / "favicon.svg").is_file()

    (brand / "header.html").write_text(CDN, encoding="utf-8")
    refused = tmp_path / "refused"
    with pytest.raises(ValueError, match="external resource"):
        _page(tmp_path, brand, outputs_dir=refused)
    assert not refused.exists()
    scanned = _page(tmp_path, brand)
    assert page_external_resource_count(scanned, brand) >= 1


def test_remote_url_in_brand_css_is_refused(tmp_path: Path) -> None:
    brand = tmp_path / "brand"
    brand.mkdir()
    (brand / "header.html").write_text(ANCHOR, encoding="utf-8")
    (brand / "brand.css").write_text(
        'header { background: url("https://cdn.example.com/a.png"); }\n',
        encoding="utf-8",
    )
    html = _page(tmp_path, brand)
    assert count_external_resource_urls(html) == 0
    assert page_external_resource_count(html, brand) == 1
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="external resource"):
        _page(tmp_path, brand, outputs_dir=out)
    assert not out.exists()


def test_chart_js_is_copied_only_when_requested(tmp_path: Path) -> None:
    out = tmp_path / "out"
    html = _page(tmp_path, None, scripts=("chart.umd.min.js",), outputs_dir=out)
    assert 'src="static/chart.umd.min.js"' in html
    assert (out / "static" / "chart.umd.min.js").is_file()
    assert (out / "static" / "licenses" / "chartjs.LICENSE.md").is_file()
    assert (out / "static" / "report.js").is_file()


def test_unknown_script_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with pytest.raises(FileNotFoundError):
        _page(tmp_path, None, scripts=("missing.js",), outputs_dir=out)
    assert not out.exists()


def _wcag_contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    def _lum(c: str) -> float:
        h = c.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        rgb = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
        lin = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    la, lb = _lum(fg_hex), _lum(bg_hex)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _check_report_css_tokens(tokens: dict[str, str]) -> list[str]:
    """Return any WCAG AA / Section 508 contrast violations across core CSS tokens."""
    failures: list[str] = []
    text_pairs = [
        ("header-fg", tokens["--brand-header-fg"], tokens["--brand-header-bg"], 4.5),
        ("header-muted", tokens["--brand-header-muted"], tokens["--brand-header-bg"], 4.5),
        ("body-ink", tokens["--brand-ink"], tokens["--brand-sheet"], 4.5),
        ("body-ink-soft", tokens["--brand-ink-soft"], tokens["--brand-sheet"], 4.5),
        ("primary-on-sheet", tokens["--brand-primary"], tokens["--brand-sheet"], 4.5),
        ("primary-inverse", tokens["--brand-primary-inverse"], tokens["--brand-primary"], 4.5),
        ("footer", tokens["--brand-footer-fg"], tokens["--brand-footer-bg"], 4.5),
        ("status-neutral", tokens["--status-neutral-fg"], tokens["--status-neutral-bg"], 4.5),
        ("status-ok", tokens["--status-ok-fg"], tokens["--status-ok-bg"], 4.5),
        ("status-warn", tokens["--status-warn-fg"], tokens["--status-warn-bg"], 4.5),
        ("status-err", tokens["--status-err-fg"], tokens["--status-err-bg"], 4.5),
        ("status-info", tokens["--status-info-fg"], tokens["--status-info-bg"], 4.5),
        ("status-ocr", tokens["--status-ocr-fg"], tokens["--status-ocr-bg"], 4.5),
        (
            "control-border",
            tokens["--brand-control-border"],
            tokens["--brand-sheet"],
            3.0,
        ),
    ]
    for name, fg, bg, minimum in text_pairs:
        ratio = _wcag_contrast_ratio(fg, bg)
        if ratio < minimum:
            failures.append(f"{name} ({fg} on {bg}) = {ratio:.2f}:1 < {minimum}:1")
    return failures


def _missing_report_css_catalog_classes(css_text: str) -> list[str]:
    """Return any `.class-name` listed in report.css's top header comment not defined below it."""
    import re

    end = css_text.find("*/")
    if not css_text.startswith("/*") or end == -1:
        return ["missing top-of-file header catalog comment"]
    header = css_text[: end + 2]
    body = css_text[end + 2 :]
    cataloged = sorted(set(re.findall(r"(?:^|[\s`>(\"])\.([a-z][a-z0-9-]*)\b", header)))
    if len(cataloged) < 20:
        return [f"catalog too sparse ({len(cataloged)} classes)"]
    defined = set(re.findall(r"\.([a-z][a-z0-9-]*)\b", body))
    return [cls for cls in cataloged if cls not in defined]


def _header_tool_css_failures(css: str) -> list[str]:
    """Header tools must beat Pico's summary color and must not paint a disc."""
    import re

    failures: list[str] = []
    icon_rule = re.search(
        r"\.app-header details\.header-disclosure > summary\.header-icon-btn,\s*"
        r"\.app-header a\.header-icon-btn\s*\{([^}]*)\}",
        css,
    )
    if icon_rule is None:
        failures.append("header icon tools do not out-specify Pico summary color")
    else:
        body = icon_rule.group(1)
        if "color: var(--brand-header-fg);" not in body:
            failures.append("header icon tools are not header foreground")
        if "background: transparent;" not in body:
            failures.append("header icon tools paint a resting fill")
        if "border-radius: 999px" in body:
            failures.append("header icon tools are circles")
    attention_rule = re.search(
        r"\.app-header details\.header-disclosure > summary\.header-icon-attention,"
        r"[^}]*\{([^}]*)\}",
        css,
    )
    if attention_rule is None or "border-color: transparent;" not in attention_rule.group(1):
        failures.append("attention paints a ring around the button")
    return failures


def test_report_css_palette_tokens_contrast_and_pico_bridge() -> None:
    """Verify Federal Navy & Ledger tokens meet WCAG AA / 508 and prove the check can fail."""
    import re

    report_css = (
        Path(__file__).resolve().parents[1] / "commissure" / "deliver" / "static" / "report.css"
    ).read_text(encoding="utf-8")
    assert count_external_resource_urls(report_css) == 0

    root_match = re.search(r":root\s*\{([^}]*)\}", report_css)
    assert root_match is not None
    tokens = {
        m.group(1): m.group(2)
        for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})\s*;", root_match.group(1))
    }

    assert tokens["--brand-header-bg"] == "#162e51"
    assert tokens["--brand-primary"] == "#005ea2"
    assert tokens["--brand-paper"] == "#f0f2f5"
    assert _check_report_css_tokens(tokens) == []

    # Prove the check fails on low-contrast UI borders (Pico's default #cfd5e2 is 1.47:1)
    # and on low-contrast badge foregrounds.
    bad_border = {**tokens, "--brand-control-border": "#cfd5e2"}
    assert any("control-border" in err for err in _check_report_css_tokens(bad_border))
    bad_badge = {**tokens, "--status-warn-fg": "#d97706"}
    assert any("status-warn" in err for err in _check_report_css_tokens(bad_badge))

    # Pico's `details summary:not([role])` (0,1,2) paints summary text in page ink.
    # Header tools have to out-specify that or every disclosure glyph is near-black
    # on the navy bar. The review link is an <a> and was never affected.
    assert _header_tool_css_failures(report_css) == []
    reverted = report_css.replace(
        ".app-header details.header-disclosure > summary.header-icon-btn",
        "summary.header-icon-btn",
        1,
    )
    assert _header_tool_css_failures(reverted) != []

    # Pico sets --pico-primary on :root:not([data-theme=dark]) (specificity 0,2,0); report.css
    # must match that selector so --brand-primary and --brand-control-border take effect.
    assert ':root:not([data-theme="dark"])' in report_css
    assert "--pico-primary: var(--brand-primary);" in report_css
    assert "--pico-form-element-border-color: var(--brand-control-border);" in report_css
    # Pin 16px root scale (overriding Pico's 125%-131.25% desktop zoom) and restrained headings.
    assert "--pico-font-size: 100%;" in root_match.group(1)
    assert re.search(r"h2\s*\{[^}]*--pico-font-size:\s*1\.06rem;", report_css) is not None
    assert re.search(r"thead th\s*\{[^}]*white-space:\s*nowrap;", report_css) is not None

    # Every class in the top-of-file component catalog must exist in report.css (prevents drift).
    assert _missing_report_css_catalog_classes(report_css) == []
    drifted_css = report_css.replace("`.badge-ok`", "`.badge-ok` | `.badge-nonexistent`", 1)
    assert _missing_report_css_catalog_classes(drifted_css) == ["badge-nonexistent"]


def test_external_links_open_in_new_tab_while_internal_links_stay_in_place(tmp_path: Path) -> None:
    """External `<a href>` citations get `target="_blank" rel="noopener noreferrer"` globally."""
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "links.html.j2").write_text(
        '<p><a id="ext-bare" href="https://www.regulations.gov/comment/CFPB-1">Source</a></p>\n'
        '<p><a id="ext-rel" href="https://example.org/doc" rel="noopener">Doc</a></p>\n'
        '<p><a id="ext-self" href="https://example.org/same" target="_self">Same</a></p>\n'
        '<p><a id="int-hash" href="#section-1">Jump</a></p>\n'
        '<p><a id="int-rel" href="../dashboard.html">Back</a></p>\n',
        encoding="utf-8",
    )
    html = render_page(
        template_dir=templates,
        template_name="links.html.j2",
        context={},
        title="Links",
    )
    assert (
        '<a id="ext-bare" href="https://www.regulations.gov/comment/CFPB-1"'
        ' target="_blank" rel="noopener noreferrer">Source</a>'
    ) in html
    assert (
        '<a id="ext-rel" href="https://example.org/doc"'
        ' rel="noopener noreferrer" target="_blank">Doc</a>'
    ) in html
    assert '<a id="ext-self" href="https://example.org/same" target="_self">Same</a>' in html
    assert '<a id="int-hash" href="#section-1">Jump</a>' in html
    assert '<a id="int-rel" href="../dashboard.html">Back</a>' in html

    report_js = (
        Path(__file__).resolve().parents[1] / "commissure" / "deliver" / "static" / "report.js"
    ).read_text(encoding="utf-8")
    assert 'a[href^="http://"], a[href^="https://"], a[href^="//"]' in report_js
