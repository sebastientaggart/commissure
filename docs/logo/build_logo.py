"""Build the Commissure logo files.

Mark geometry (iteration #1):
r=40 semicircle, legs to x=+-44 (36 long), three bars x=-20..20 at y=-20,0,20,
stroke 8, round caps. Wordmark: Inter Medium outlined to paths with GPOS kerning
(pair adjustment, format 1 and 2 lookups).
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FONT_PATHS = (
    Path("/usr/share/fonts/opentype/inter/Inter-Medium.otf"),
    Path("/Library/Fonts/Inter-Medium.otf"),
    Path.home() / "Library/Fonts/Inter-Medium.otf",
)

INK, ACC = "#1f2328", "#d9480f"
INK_D, ACC_D = "#e6edf3", "#ff7b3d"

BADGES: tuple[tuple[str, str, str, str], ...] = (
    ("badge-python.svg", "python", "3.12+", "#3776ab"),
    ("badge-storage.svg", "storage", "SQLite WAL · FTS5", "#0f80cc"),
    ("badge-schemas.svg", "schemas", "Pydantic v2", "#d61f5a"),
    ("badge-egress.svg", "egress", "offline by default", "#2b8a3e"),
    ("badge-license.svg", "license", "MIT", ACC),
)

# Exact Verdana 11px advance widths (from TrueType hmtx/cmap at 11px).
_VERDANA_11_WIDTHS: dict[str, float] = {
    " ": 3.87,
    "+": 9.0,
    "-": 5.0,
    ".": 4.0,
    "1": 6.99,
    "2": 6.99,
    "3": 6.99,
    "5": 6.99,
    "A": 7.52,
    "F": 6.32,
    "I": 4.63,
    "L": 6.12,
    "M": 9.27,
    "P": 6.63,
    "Q": 8.66,
    "S": 7.52,
    "T": 6.78,
    "W": 10.88,
    "a": 6.61,
    "b": 6.85,
    "c": 5.73,
    "d": 6.85,
    "e": 6.55,
    "f": 3.87,
    "g": 6.85,
    "h": 6.96,
    "i": 3.02,
    "l": 3.02,
    "m": 10.7,
    "n": 6.96,
    "o": 6.68,
    "p": 6.85,
    "r": 4.69,
    "s": 5.73,
    "t": 4.33,
    "u": 6.96,
    "v": 6.51,
    "y": 6.51,
    "·": 4.0,
}

STYLE = f"""  <style>
    :root {{ --ink:{INK}; --accent:{ACC}; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --ink:{INK_D}; --accent:{ACC_D}; }} }}
    .s {{ stroke:var(--ink); fill:none; stroke-linecap:round; stroke-linejoin:round; }}
    .b {{ stroke:var(--accent); fill:none; stroke-linecap:round; }}
    .w {{ fill:var(--ink); }}
  </style>
"""


def mark(tx: float = 0, ty: float = 0, scale: float = 1.0, sw: int = 8) -> str:
    return f"""  <g transform="translate({tx},{ty}) scale({scale})" style="stroke-width:{sw}">
    <path class="s" d="M -44 -40 H -80 A 40 40 0 0 0 -80 40 H -44"/>
    <path class="s" d="M 44 -40 H 80 A 40 40 0 0 1 80 40 H 44"/>
    <path class="b" d="M -20 -20 H 20"/>
    <path class="b" d="M -20 0 H 20"/>
    <path class="b" d="M -20 20 H 20"/>
  </g>
"""


def _kern_table(font: object) -> dict[tuple[str, str], int]:
    """Flatten GPOS pair-adjustment (format 1 and 2) into {(a, b): xAdvance}."""
    k: dict[tuple[str, str], int] = {}
    if "GPOS" not in font:  # type: ignore[operator]
        return k
    gpos = font["GPOS"].table  # type: ignore[index]
    for lookup in gpos.LookupList.Lookup:
        subs = lookup.SubTable
        if lookup.LookupType == 9:
            subs = [s.ExtSubTable for s in subs]
        for st in subs:
            if getattr(st, "LookupType", lookup.LookupType) != 2:
                continue
            cov = st.Coverage.glyphs
            if st.Format == 1:
                for g1, ps in zip(cov, st.PairSet, strict=False):
                    for pvr in ps.PairValueRecord:
                        v = pvr.Value1
                        if v and v.XAdvance:
                            k.setdefault((g1, pvr.SecondGlyph), v.XAdvance)
            elif st.Format == 2:
                c1 = st.ClassDef1.classDefs
                c2 = st.ClassDef2.classDefs
                for g1 in cov:
                    cls1 = c1.get(g1, 0)
                    rec1 = st.Class1Record[cls1]
                    for g2, cls2 in c2.items():
                        v = rec1.Class2Record[cls2].Value1
                        if v and v.XAdvance:
                            k.setdefault((g1, g2), v.XAdvance)
    return k


def _outline_with_font(font_path: Path, text: str, size: float, letter_spacing: float = 0.0) -> str:
    """Return SVG path `d` for `text` at font size `size` (px), baseline at y=0."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    font = TTFont(str(font_path))
    upm = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    gs = font.getGlyphSet()
    hmtx = font["hmtx"]
    kern = _kern_table(font)

    s = size / upm
    x = 0.0
    parts: list[str] = []
    glyphs = [cmap[ord(c)] for c in text]
    for i, g in enumerate(glyphs):
        pen = SVGPathPen(gs)
        tp = TransformPen(pen, (s, 0, 0, -s, x, 0))
        gs[g].draw(tp)
        d = pen.getCommands()
        if d:
            parts.append(d)
        x += hmtx[g][0] * s
        if i + 1 < len(glyphs):
            x += kern.get((g, glyphs[i + 1]), 0) * s + letter_spacing
    return " ".join(parts)


def _extract_existing_word_path(svg_path: Path) -> str:
    text = svg_path.read_text(encoding="utf-8")
    m = re.search(r'<path class="w"[^>]*\bd="([^"]+)"', text)
    if not m:
        raise RuntimeError(f"Could not find wordmark path in {svg_path}")
    return m.group(1)


def _path_bbox(d: str) -> tuple[float, float, float, float]:
    tokens = re.findall(r"[A-Za-z]|[-+]?\d*\.\d+|[-+]?\d+", d)
    xs: list[float] = []
    ys: list[float] = []
    cmd: str | None = None
    i = 0
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
        if cmd in ("M", "L"):
            xs.append(float(tokens[i]))
            ys.append(float(tokens[i + 1]))
            i += 2
        elif cmd == "H":
            xs.append(float(tokens[i]))
            i += 1
        elif cmd == "V":
            ys.append(float(tokens[i]))
            i += 1
        elif cmd == "C":
            for j in range(0, 6, 2):
                xs.append(float(tokens[i + j]))
                ys.append(float(tokens[i + j + 1]))
            i += 6
        else:
            i += 1
    return min(xs), min(ys), max(xs), max(ys)


def _resolve_font(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"Font file not found: {p}")
        return p
    env_font = os.environ.get("INTER_MEDIUM_OTF")
    if env_font:
        p = Path(env_font)
        if p.is_file():
            return p
    for candidate in DEFAULT_FONT_PATHS:
        if candidate.is_file():
            return candidate
    return None


def _text_width_11px(text: str) -> float:
    return sum(_VERDANA_11_WIDTHS[ch] for ch in text)


def write_svg(out_dir: Path, name: str, body: str, vb_w: int, vb_h: int) -> None:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w} {vb_h}" '
        f'width="{vb_w}" height="{vb_h}" role="img" aria-label="Commissure">\n'
        f"{STYLE}{body}</svg>\n"
    )
    (out_dir / name).write_text(svg, encoding="utf-8")


def write_badge_svg(out_dir: Path, name: str, label: str, value: str, color: str) -> None:
    lw = _text_width_11px(label)
    vw = _text_width_11px(value)
    l_len = round(lw * 10)
    v_len = round(vw * 10)
    l_box = round(lw) + 12
    v_box = round(vw) + 12
    total_w = l_box + v_box
    l_cx = round((l_box / 2 + 0.5) * 10)
    v_cx = round((l_box + v_box / 2 - 0.5) * 10)
    title = f"{label}: {value}"
    font = "Verdana,Geneva,DejaVu Sans,sans-serif"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20" '
        f'viewBox="0 0 {total_w} 20" role="img" aria-label="{title}">\n'
        f"  <title>{title}</title>\n"
        f"  <style>\n"
        f"    :root {{ --l:#24292f; --r:{color}; }}\n"
        f"    @media (prefers-color-scheme: dark) {{ :root {{ --l:#30363d; --r:{color}; }} }}\n"
        f"  </style>\n"
        f'  <linearGradient id="s" x2="0" y2="100%">\n'
        f'    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>\n'
        f'    <stop offset="1" stop-opacity=".1"/>\n'
        f"  </linearGradient>\n"
        f'  <clipPath id="c">\n'
        f'    <rect width="{total_w}" height="20" rx="3" fill="#fff"/>\n'
        f"  </clipPath>\n"
        f'  <g clip-path="url(#c)">\n'
        f'    <rect width="{l_box}" height="20" fill="var(--l)"/>\n'
        f'    <rect x="{l_box}" width="{v_box}" height="20" fill="var(--r)"/>\n'
        f'    <rect width="{total_w}" height="20" fill="url(#s)"/>\n'
        f"  </g>\n"
        f'  <g fill="#fff" text-anchor="middle" font-family="{font}" '
        f'text-rendering="geometricPrecision" font-size="110">\n'
        f'    <text aria-hidden="true" x="{l_cx}" y="150" fill="#010101" fill-opacity=".3" '
        f'transform="scale(.1)" textLength="{l_len}">{label}</text>\n'
        f'    <text x="{l_cx}" y="140" transform="scale(.1)" fill="#fff" '
        f'textLength="{l_len}">{label}</text>\n'
        f'    <text aria-hidden="true" x="{v_cx}" y="150" fill="#010101" fill-opacity=".3" '
        f'transform="scale(.1)" textLength="{v_len}">{value}</text>\n'
        f'    <text x="{v_cx}" y="140" transform="scale(.1)" fill="#fff" '
        f'textLength="{v_len}">{value}</text>\n'
        f"  </g>\n"
        f"</svg>\n"
    )
    (out_dir / name).write_text(svg, encoding="utf-8")


def build_all(out_dir: Path = HERE, font_path: Path | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pad = 8.0

    # 1. mark.svg: scale=1.0, sw=8 -> ink is 248x88; with 8px padding -> 264x104
    write_svg(out_dir, "mark.svg", mark(132, 52, 1.0, sw=8), 264, 104)

    # 2. favicon.svg: square 128x128, centered mark at scale=0.5 with heavier stroke=14
    write_svg(out_dir, "favicon.svg", mark(64, 64, 0.5, sw=14), 128, 128)

    # 3. logo.svg: inline horizontal lockup (tight 8px margin -> 449x71)
    if font_path is not None:
        d_inline = _outline_with_font(font_path, "commissure", 44, 0.5)
    else:
        d_inline = _extract_existing_word_path(out_dir / "logo.svg")
    _, _, wx1, _ = _path_bbox(d_inline)
    m_scale = 0.62
    m_half_w = 124 * m_scale
    m_cx = round(pad + m_half_w, 2)
    cy = 35.5
    text_x = round(m_cx + m_half_w + 20, 2)
    vb_w_inline = round(text_x + wx1 + pad)
    vb_h_inline = 71
    body_inline = (
        mark(m_cx, cy, m_scale, sw=8)
        + f'  <path class="w" transform="translate({text_x:.2f},{cy + 16})" d="{d_inline}"/>\n'
    )
    write_svg(out_dir, "logo.svg", body_inline, vb_w_inline, vb_h_inline)

    # 4. logo-stacked.svg: centered hero lockup (tight 8px margin -> 300x151)
    if font_path is not None:
        d_stacked = _outline_with_font(font_path, "commissure", 40, 6.0)
    else:
        d_stacked = _extract_existing_word_path(out_dir / "logo-stacked.svg")
    sx0, _, sx1, _ = _path_bbox(d_stacked)
    vb_w_stacked = 300
    vb_h_stacked = 151
    mark_cy = 38.68
    word_tx = round((vb_w_stacked - (sx0 + sx1)) / 2, 2)
    word_ty = 142.68
    body_stacked = (
        mark(vb_w_stacked / 2, mark_cy, 0.7, sw=8)
        + f'  <path class="w" transform="translate({word_tx:.2f},{word_ty:.2f})" '
        f'd="{d_stacked}"/>\n'
    )
    write_svg(out_dir, "logo-stacked.svg", body_stacked, vb_w_stacked, vb_h_stacked)

    # 5. Status/posture badges for README.md
    for name, label, value, color in BADGES:
        write_badge_svg(out_dir, name, label, value, color)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Commissure SVG logo assets.")
    parser.add_argument("--font", help="Path to Inter-Medium.otf (optional if SVGs exist).")
    parser.add_argument("--out", default=str(HERE), help="Output directory.")
    args = parser.parse_args()
    build_all(Path(args.out), _resolve_font(args.font))


if __name__ == "__main__":
    main()
