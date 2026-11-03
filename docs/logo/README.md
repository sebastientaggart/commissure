# Commissure Logo Assets

All files are self-contained SVGs with dark-mode support via `@media (prefers-color-scheme: dark)` and the wordmark outlined to `<path>` elements so no external fonts are required.

| File | ViewBox | Use |
| :--- | :--- | :--- |
| `logo-stacked.svg` | `300 × 151` | Centered hero lockup (mark stacked above wordmark) |
| `logo.svg` | `449 × 71` | Horizontal inline lockup (mark + wordmark) |
| `mark.svg` | `264 × 104` | Standalone mark (8px margin) |
| `favicon.svg` | `128 × 128` | Square favicon mark with heavier stroke (`stroke-width: 14` at `0.5` scale) for 16–64px icons |
| `badge-*.svg` | `var × 20` | Self-contained README status/posture badges (`python`, `storage`, `schemas`, `egress`, `license`) |
| `build_logo.py` | — | Regenerates all logo and badge SVGs (`python3 docs/logo/build_logo.py`) |

## Geometry & Palette

- **Mark geometry:** `r = 40` semicircles, `36px` horizontal legs (`x = ±44..±80`), three commissural bridge bars (`x = -20..20` at `y = -20, 0, 20`), `8px` stroke (`14px` in `favicon.svg`), round caps and joins.
- **Wordmark:** Inter Medium (`SIL OFL-1.1`), outlined to SVG `<path>` contours with GPOS kerning.
- **Colors:**
  - Light mode: ink `#1f2328`, accent `#d9480f`
  - Dark mode (`prefers-color-scheme: dark`): ink `#e6edf3`, accent `#ff7b3d`

## Usage Snippets

Centered stacked header (`README.md`):

```html
<p align="center">
  <img src="docs/logo/logo-stacked.svg" alt="Commissure" width="280">
</p>
```

Left-aligned inline header:

```html
<p align="left">
  <img src="docs/logo/logo.svg" alt="Commissure" width="380">
</p>
```

HTML favicon link (using vendored `static/favicon.svg`):

```html
<link rel="icon" type="image/svg+xml" href="static/favicon.svg">
```
