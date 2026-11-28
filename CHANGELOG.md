# Changelog

Changes to Commissure core that a solution author may need to act on. Solution development environments print what this file gained when they move to a new core revision, so an entry is written for the person updating a recipe, not for core maintainers.

- Entries go under `## Unreleased` as they land; a release renames that heading to `## <version>`.
- **Upgrading:** something a solution must change (a renamed API, a stricter check, a moved file), with the exact change to make. A core change that breaks a solution never lands without one.
- **Added:** a new capability a solution may want to adopt, such as a helper graduated to `commissure/patterns/` that replaces code in a recipe.
- History before 0.3.0 is in git.

## Unreleased

- **Added:** Default chart tokens, zero-JS HTML chart primitives, and Chart.js configuration in `commissure/deliver/`. `report.css` now defines `:root` chart tokens (`--chart-cat-1` to `--chart-cat-8`, `--chart-cat-rest`, `--chart-series`, `--chart-seq-1` to `--chart-seq-5`, `--chart-div-neg` / `--chart-div-mid` / `--chart-div-pos`, `--chart-wash`, `--chart-band`, `--chart-grid`, `--chart-axis-ink`, `--chart-label-on-dark`, `--chart-label-on-light`, overridable in `recipe/brand/brand.css`) and HTML chart classes (`.chart-segbar > .chart-seg`, `.chart-on-dark`, `.chart-on-light`, `.chart-legend > li > .chart-swatch`, `.chart-bars`, `.chart-canvas`, `.chart-canvas-tall`). Passing `"chart.umd.min.js"` in `render_page(..., scripts=...)` automatically includes `static/charts.js`, which applies token-backed `Chart.defaults`, colors datasets from `--chart-series` or `--chart-cat-*`, and exposes `window.CommissureCharts` (`color`, `colorFor`, `capLabels`, `referenceLines`). Recipes can delete their page-local chart palettes and Chart.js boilerplate; custom `brand.css` palettes can be validated in tests with `commissure.deliver.html.check_chart_palette`.
