"""Outputs for this recipe.

Placeholder. An ordinary `run`-subject (or `group`-subject) stage that receives upstream
payloads in memory (`group.items` and `group.excluded`), writes rendered files into
`ctx.outputs` (`<workspace>/outputs/<stage>/<run_id>/`), verifies structural integrity
(exists, non-zero size, expected page/sheet/row counts, no external resource URLs), and
returns a `schema.py` record describing the output. Stages never read the database
directly or write to source inputs. Declare template files on `Stage(..., assets=(...))`.

HTML pages go through `render_page` (`commissure.api`); never hand-roll a full
`<!DOCTYPE html>` shell or link `pico.min.css` without `report.css`. The body template
is a fragment built from the primitives indexed at the top of
`commissure/deliver/static/report.css` (`.kpis`, `.badge-*`, `.notice-*`, `.form-row`,
`.section-toolbar`, `table[data-sortable]`) rather than page-local `<style>` themes.
Optional client chrome (literal HTML, plus `--brand-*` `:root` overrides in `brand.css`)
lives beside it and is listed in `assets` so an edit marks the stage stale:

    recipe/brand/header.html
    recipe/brand/footer.html
    recipe/brand/brand.css
    recipe/brand/logo.svg      # only when there is a file the solution may ship

Missing brand files fall back to the default header and footer. `report.js` sorts
`table[data-sortable]` and filters rows from `input[data-filter]`. Pass
`scripts=("chart.umd.min.js",)` or `scripts=("cytoscape.min.js",)` only on a page that draws
that figure, and inline the data. Do not `fetch`, and do not load htmx in a file output.
"""
