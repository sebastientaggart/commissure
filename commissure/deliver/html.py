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
(``chart.umd.min.js``, ``cytoscape.min.js``). Passing ``chart.umd.min.js``
automatically adds first-party ``charts.js`` right after it, which reads the
``--chart-*`` tokens from ``report.css`` (or ``brand/brand.css``), configures
``Chart.defaults``, and exposes ``window.CommissureCharts`` (``color``,
``colorFor``, ``capLabels``, ``referenceLines``). Inline chart data in the body:

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

import math
import re
import shutil
from collections.abc import Mapping, Sequence
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


def _expand_scripts(scripts: tuple[str, ...]) -> tuple[str, ...]:
    """Attach first-party ``charts.js`` after ``chart.umd.min.js`` when requested."""
    expanded: list[str] = []
    for name in scripts:
        expanded.append(name)
        if (
            name == "chart.umd.min.js"
            and "charts.js" not in scripts
            and "charts.js" not in expanded
        ):
            expanded.append("charts.js")
    return tuple(expanded)


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
    effective_scripts = _expand_scripts(scripts)
    for name in effective_scripts:
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
            scripts=effective_scripts,
            static_prefix=clean_prefix,
        )
    )
    if outputs_dir is not None:
        hits = page_external_resource_count(html, brand_dir if brand_css else None)
        if hits:
            raise ValueError(f"refusing to write assets: {hits} external resource URL(s)")
        _copy_assets(outputs_dir, brand_dir, effective_scripts)
    return html


# Viénot 1999 / Machado 2009 full-severity linear-RGB color-vision deficiency matrices.
_CVD_LINEAR_RGB: dict[str, tuple[tuple[float, float, float], ...]] = {
    "protanopia": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deuteranopia": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritanopia": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}


def _hex_to_linear_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        raise ValueError(f"expected #RRGGBB hex color, got {hex_color!r}")
    srgb = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    lin = tuple(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in srgb)
    return (lin[0], lin[1], lin[2])


def _linear_rgb_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    l_cone = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_cone = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_cone = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = math.cbrt(l_cone), math.cbrt(m_cone), math.cbrt(s_cone)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b_ = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return (L, a, b_)


def _oklab_de100(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    return 100.0 * math.sqrt(sum((x - y) ** 2 for x, y in zip(lab1, lab2, strict=True)))


def _wcag_contrast(hex_a: str, hex_b: str) -> float:
    def _lum(c: str) -> float:
        r, g, b = _hex_to_linear_rgb(c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    la, lb = _lum(hex_a), _lum(hex_b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def check_chart_palette(
    categorical: Sequence[str],
    *,
    label_on_dark: str = "#ffffff",
    label_on_light: str = "#1b1b1b",
    status_colors: Sequence[str] = (),
    all_pairs_leading: int = 4,
    lightness_range: tuple[float, float] = (0.43, 0.77),
    min_chroma: float = 0.10,
    min_normal_de: float = 15.0,
    min_cvd_de: float = 8.0,
    min_status_de: float = 8.0,
) -> list[str]:
    """Validate a categorical chart palette for OKLCH range, label contrast, and CVD separation.

    Returns a list of violation descriptions (empty when valid). Both core tests and
    solution tests with custom ``brand/brand.css`` overrides can call this function.
    """
    if len(categorical) < 2:
        return ["categorical palette must contain at least 2 colors"]

    failures: list[str] = []
    min_l, max_l = lightness_range
    normal_labs: list[tuple[float, float, float]] = []
    cvd_labs: dict[str, list[tuple[float, float, float]]] = {k: [] for k in _CVD_LINEAR_RGB}
    status_labs = [_linear_rgb_to_oklab(*_hex_to_linear_rgb(s)) for s in status_colors]

    for idx, color in enumerate(categorical, 1):
        r, g, b = _hex_to_linear_rgb(color)
        lab = _linear_rgb_to_oklab(r, g, b)
        normal_labs.append(lab)
        L, a, b_ = lab
        chroma = math.hypot(a, b_)
        if not (min_l <= L <= max_l):
            failures.append(f"slot {idx} ({color}) OKLCH L={L:.3f} outside [{min_l}, {max_l}]")
        if chroma < min_chroma:
            failures.append(f"slot {idx} ({color}) OKLCH C={chroma:.3f} < {min_chroma}")

        best_label_contrast = max(
            _wcag_contrast(color, label_on_dark),
            _wcag_contrast(color, label_on_light),
        )
        if best_label_contrast < 4.5:
            failures.append(
                f"slot {idx} ({color}) in-fill label contrast {best_label_contrast:.2f}:1 < 4.5:1"
            )

        for s_hex, s_lab in zip(status_colors, status_labs, strict=True):
            dist = _oklab_de100(lab, s_lab)
            if dist < min_status_de:
                failures.append(
                    f"slot {idx} ({color}) too close to status color {s_hex} "
                    f"(OKLab dE*100={dist:.1f} < {min_status_de})"
                )

        for kind, mat in _CVD_LINEAR_RGB.items():
            sr = max(0.0, min(1.0, mat[0][0] * r + mat[0][1] * g + mat[0][2] * b))
            sg = max(0.0, min(1.0, mat[1][0] * r + mat[1][1] * g + mat[1][2] * b))
            sb = max(0.0, min(1.0, mat[2][0] * r + mat[2][1] * g + mat[2][2] * b))
            cvd_labs[kind].append(_linear_rgb_to_oklab(sr, sg, sb))

    def _check_pair(i: int, j: int, scope: str) -> None:
        norm_de = _oklab_de100(normal_labs[i], normal_labs[j])
        if norm_de < min_normal_de:
            failures.append(
                f"{scope} slots ({i + 1}, {j + 1}) ({categorical[i]}, {categorical[j]}) "
                f"normal dE*100={norm_de:.1f} < {min_normal_de}"
            )
        for kind, labs in cvd_labs.items():
            cvd_de = _oklab_de100(labs[i], labs[j])
            if cvd_de < min_cvd_de:
                failures.append(
                    f"{scope} slots ({i + 1}, {j + 1}) ({categorical[i]}, {categorical[j]}) "
                    f"{kind} dE*100={cvd_de:.1f} < {min_cvd_de}"
                )

    for i in range(len(categorical) - 1):
        _check_pair(i, i + 1, "adjacent")

    lead = min(all_pairs_leading, len(categorical))
    for i in range(lead):
        for j in range(i + 2, lead):
            _check_pair(i, j, f"all-pairs(1..{lead})")

    return failures


def check_chart_sequential_ramp(
    ramp: Sequence[str],
    *,
    label_on_dark: str = "#ffffff",
    label_on_light: str = "#1b1b1b",
    min_step_dl: float = 0.08,
) -> list[str]:
    """Validate a light-to-dark sequential chart ramp for monotonic OKLCH lightness and labels."""
    if len(ramp) < 3:
        return ["sequential ramp must contain at least 3 steps"]
    failures: list[str] = []
    lightnesses: list[float] = []
    for idx, color in enumerate(ramp, 1):
        L, _, _ = _linear_rgb_to_oklab(*_hex_to_linear_rgb(color))
        lightnesses.append(L)
        best_contrast = max(
            _wcag_contrast(color, label_on_dark),
            _wcag_contrast(color, label_on_light),
        )
        if best_contrast < 4.5:
            failures.append(
                f"seq-{idx} ({color}) in-fill label contrast {best_contrast:.2f}:1 < 4.5:1"
            )
    for i in range(len(lightnesses) - 1):
        dl = lightnesses[i] - lightnesses[i + 1]
        if dl < min_step_dl:
            failures.append(
                f"seq-{i + 1}..seq-{i + 2} ({ramp[i]} -> {ramp[i + 1]}) "
                f"lightness drop dL={dl:.3f} < {min_step_dl}"
            )
    return failures
