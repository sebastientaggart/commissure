"""Writes tests/fixtures/ocr_corpus/: image-only PDFs with known content for the `ocr` parser.

Pages are rendered with Pillow's bundled default font and saved as image-only PDFs (no text
layer), the way a scanner or a "print to image" export produces them. Rerun with
`uv run python tests/fixtures/make_ocr_corpus.py` after editing and review the diff.

- typed_letter.pdf: two pages of clean typed text, the sentences in TYPED below. Every
  page should be recognised.
- blank_scan.pdf: one page of faint noise with no writing. Recognition must yield nothing,
  so the page stays unreadable with `ocr_no_text`; this is the fixture that proves the
  check can fail.

And tests/fixtures/ocr_rotated/, kept apart so the corpus counts above stay fixed:

- upside_down_letter.pdf: the first page of typed_letter.pdf turned 180 degrees, as when a
  page's content is drawn upside down inside it. Read as rendered it fails on confidence;
  the parser must ask OSD for the orientation, turn it, and recognise it.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "ocr_corpus"
ROTATED = Path(__file__).parent / "ocr_rotated"
DPI = 200
PAGE = (int(8.5 * DPI), int(11 * DPI))

TYPED = [
    [
        "Budget Justification for Fiscal Year 2025",
        "Grant Number 90ZZ000001 Continuation Application",
        "Personnel: three teachers at 1.0 FTE each, total 187,500 dollars.",
        "Fringe Benefits: 28 percent of salaries, total 52,500 dollars.",
        "Travel: one national conference for two staff, 4,200 dollars.",
    ],
    [
        "Non-Federal Share",
        "Volunteer hours valued at 31.80 per hour, total 61,000 dollars.",
        "Donated classroom space from the county, 24,000 dollars.",
        "Total non-federal share: 85,000 dollars, which is twenty percent.",
    ],
]


def typed_page(lines: list[str]) -> Image.Image:
    img = Image.new("L", PAGE, 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=44)
    y = 220
    for line in lines:
        draw.text((180, y), line, font=font, fill=0)
        y += 90
    return img


def noise_page(seed: int = 7) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("L", (PAGE[0] // 4, PAGE[1] // 4), 250)
    px = img.load()
    for _ in range(4000):
        px[rng.randrange(img.width), rng.randrange(img.height)] = rng.randrange(200, 245)
    return img.resize(PAGE)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    pages = [typed_page(p) for p in TYPED]
    pages[0].save(OUT / "typed_letter.pdf", save_all=True, append_images=pages[1:], resolution=DPI)
    noise_page().save(OUT / "blank_scan.pdf", resolution=DPI)
    ROTATED.mkdir(exist_ok=True)
    pages[0].rotate(180).save(ROTATED / "upside_down_letter.pdf", resolution=DPI)
    for p in sorted([*OUT.glob("*.pdf"), *ROTATED.glob("*.pdf")]):
        print(f"wrote {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
