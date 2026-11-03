"""One offline HTML shell for recipe pages.

A recipe body template is a fragment starting at ``<h2>``. ``render_page`` wraps
it in the shared sticky ``.app-header`` (title, optional info disclosure, and
optional ``brand/header.html`` slot) and ``.app-footer`` (``brand/footer.html``
or the default Commissure footer), plus Pico, first-party ``report.css`` and
``report.js``, and optional ``brand/brand.css``. Header and footer are literal
HTML, not Jinja templates. Other files in the brand directory are copied to
``static/brand/`` when ``outputs_dir`` is set. Nested deliverables sharing a
parent ``static/`` directory pass ``static_prefix="../static"``.

``table[data-sortable]`` sorts on a header click. ``input[data-filter]`` is a
CSS selector for the table whose rows it filters. Both work from the file as
opened, with no server. Without the script the table is still the full table.

Pass ``scripts`` to copy an extra vendored file next to the page
(``chart.umd.min.js``, ``cytoscape.min.js``). Inline its data in the body:

    <script type="application/json" id="series">{{ series | tojson }}</script>

Do not ``fetch``. htmx is for a later in-process interface, not for these files.

An output directory is a static site root: the HTML file, ``static/``, and any
sibling data files use relative URLs. Scan with ``page_external_resource_count``
before passing ``outputs_dir``. A remote stylesheet, script, image, ``url()``,
or ``@import`` is refused and nothing is written. An ``<a href>`` to another
site is a citation and is allowed. ``render_page`` also refuses to copy assets
when that count is non-zero, so a caller cannot write the shell of a bad page.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

_PACKAGE = Path(__file__).resolve().parent
STATIC_DIR = _PACKAGE / "static"
_SHELL = _PACKAGE / "templates" / "page.html.j2"

# Always copied. Licenses travel only for the third-party files among them.
_BASE_ASSETS = ("pico.min.css", "favicon.svg", "report.css", "report.js")
_LICENSE_PREFIX = {
    "pico.min.css": "pico",
    "chart.umd.min.js": "chartjs",
    "cytoscape.min.js": "cytoscape",
    "htmx.min.js": "htmx",
}
_INLINE_BRAND = frozenset({"header.html", "footer.html"})

DEFAULT_PAGE_FOOTER = Markup(
    "<p>&copy; Commissure &middot; Local-first document and data operations</p>"
)

# A loaded asset that points at another host. An <a href> is not a loaded asset.
_EXTERNAL_RESOURCE_RE = re.compile(
    r"""(?ix)
    (?:
        <\s*(?:script|img|iframe|source|embed|audio|video)\b[^>]*?\bsrc\s*=\s*["']?\s*(?:https?:|//)
      | <\s*link\b[^>]*?\bhref\s*=\s*["']?\s*(?:https?:|//)
      | <\s*object\b[^>]*?\bdata\s*=\s*["']?\s*(?:https?:|//)
      | @import\s+(?:url\(\s*)?["']?\s*(?:https?:|//)
      | url\(\s*["']?\s*(?:https?:|//)
    )
    """
)

_EXTERNAL_ANCHOR_RE = re.compile(
    r"""(?ix)
    <\s*a\b
    (?=[^>]*?\bhref\s*=\s*(?:["']\s*(?:https?:|//)|(?:https?:|//)))
    [^>]*>
    """
)
_TARGET_ATTR_RE = re.compile(r"""(?i)\btarget\s*=\s*(?:["']([^"']*)["']|([^\s>]+))""")
_REL_ATTR_RE = re.compile(r"""(?i)\brel\s*=\s*(?:["']([^"']*)["']|([^\s>]+))""")


def external_links_open_in_new_tab(html: str) -> str:
    """Ensure external `<a href="http(s)://...">` links open in a new tab safely."""

    def _upgrade_anchor(match: re.Match[str]) -> str:
        tag = match.group(0)
        target_m = _TARGET_ATTR_RE.search(tag)
        if target_m is None:
            tag = tag[:-1] + ' target="_blank">'
            target_val = "_blank"
        else:
            target_val = (target_m.group(1) or target_m.group(2) or "").strip().lower()

        if target_val == "_blank":
            rel_m = _REL_ATTR_RE.search(tag)
            if rel_m is None:
                tag = tag[:-1] + ' rel="noopener noreferrer">'
            else:
                raw_rel = rel_m.group(1) if rel_m.group(1) is not None else (rel_m.group(2) or "")
                parts = [p for p in raw_rel.split() if p]
                lower_parts = {p.lower() for p in parts}
                for req in ("noopener", "noreferrer"):
                    if req not in lower_parts:
                        parts.append(req)
                merged_rel = " ".join(parts)
                tag = tag[: rel_m.start()] + f'rel="{merged_rel}"' + tag[rel_m.end() :]
        return tag

    return _EXTERNAL_ANCHOR_RE.sub(_upgrade_anchor, html)


def count_external_resource_urls(text: str) -> int:
    """Count remote stylesheets, scripts, images, CSS ``url()``, and ``@import``."""
    return len(_EXTERNAL_RESOURCE_RE.findall(text))


def page_external_resource_count(html: str, brand_dir: Path | None = None) -> int:
    """Hits in the page, plus ``brand.css`` (it is linked, not inlined)."""
    total = count_external_resource_urls(html)
    if brand_dir is not None:
        css = brand_dir / "brand.css"
        if css.is_file():
            total += count_external_resource_urls(css.read_text(encoding="utf-8"))
    return total


def _fragment(brand_dir: Path | None, name: str) -> Markup:
    if brand_dir is None:
        return Markup("")
    path = brand_dir / name
    if not path.is_file():
        return Markup("")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return Markup("")
    return Markup(text)


def _require_script(name: str) -> Path:
    if name != Path(name).name or not name.endswith(".js") or name.startswith("."):
        raise ValueError(f"script must be a vendored file name, not {name!r}")
    path = STATIC_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"vendored script {name} is not in {STATIC_DIR}")
    return path


def _copy_licenses(destination: Path, asset_names: tuple[str, ...]) -> None:
    source = STATIC_DIR / "licenses"
    if not source.is_dir():
        return
    prefixes = {_LICENSE_PREFIX[name] for name in asset_names if name in _LICENSE_PREFIX}
    if not prefixes:
        return
    licenses = destination / "licenses"
    licenses.mkdir(exist_ok=True)
    for lic in source.iterdir():
        if lic.is_file() and any(lic.name.startswith(prefix) for prefix in prefixes):
            shutil.copy2(lic, licenses / lic.name)


def _copy_assets(
    outputs_dir: Path,
    brand_dir: Path | None,
    scripts: tuple[str, ...],
) -> None:
    static = outputs_dir / "static"
    static.mkdir(parents=True, exist_ok=True)
    names = _BASE_ASSETS + scripts
    for name in names:
        shutil.copy2(STATIC_DIR / name, static / name)
    _copy_licenses(static, names)
    if brand_dir is None:
        return
    brand_out = static / "brand"
    for path in sorted(brand_dir.iterdir()):
        if not path.is_file() or path.name in _INLINE_BRAND:
            continue
        brand_out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, brand_out / path.name)


def _clean_static_prefix(static_prefix: str) -> str:
    prefix = static_prefix.strip().rstrip("/")
    if not prefix or "://" in prefix or prefix.startswith(("/", "\\")):
        raise ValueError(
            "static_prefix must be a relative path (e.g. 'static' or '../static'), "
            f"not {static_prefix!r}"
        )
    return prefix


def render_page(
    *,
    template_dir: Path,
    template_name: str,
    context: Mapping[str, Any],
    title: str,
    subtitle: str = "",
    brand_dir: Path | None = None,
    scripts: tuple[str, ...] = (),
    outputs_dir: Path | None = None,
    static_prefix: str = "static",
) -> str:
    """Render the shell around ``template_name``. Copy assets when ``outputs_dir`` is set.

    Callers that quarantine a page with a remote asset should omit ``outputs_dir``,
    scan the returned HTML, and call again with ``outputs_dir`` only when the count
    is zero. Passing ``outputs_dir`` for a page that fails the scan raises and
    writes nothing.
    """
    clean_prefix = _clean_static_prefix(static_prefix)
    for name in scripts:
        _require_script(name)
    if brand_dir is not None and not brand_dir.is_dir():
        raise FileNotFoundError(f"brand directory {brand_dir} does not exist")

    body_env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
    )
    body = Markup(body_env.get_template(template_name).render(**context))
    brand_header = _fragment(brand_dir, "header.html")
    brand_footer = _fragment(brand_dir, "footer.html")
    footer_html = brand_footer or DEFAULT_PAGE_FOOTER
    brand_css = brand_dir is not None and (brand_dir / "brand.css").is_file()
    if brand_css and not (brand_dir / "brand.css").read_text(encoding="utf-8").strip():
        brand_css = False

    shell_env = Environment(
        loader=FileSystemLoader(str(_SHELL.parent)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
    )
    html = external_links_open_in_new_tab(
        shell_env.get_template(_SHELL.name).render(
            title=title,
            subtitle=subtitle,
            body=body,
            brand_header=brand_header,
            footer_html=footer_html,
            brand_css=brand_css,
            scripts=scripts,
            static_prefix=clean_prefix,
        )
    )
    if outputs_dir is not None:
        hits = page_external_resource_count(html, brand_dir if brand_css else None)
        if hits:
            raise ValueError(f"refusing to write assets: {hits} external resource URL(s)")
        _copy_assets(outputs_dir, brand_dir, scripts)
    return html
