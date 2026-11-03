"""Entry point: `commissure run --step <stage>` loads this file (from inside the solution
folder, or with `--recipe <solution>/recipe`).

Define STAGES, in order, as a list of Stage. Import sibling modules relatively. Each
stage's schema must be a pydantic model with `model_config = ConfigDict(extra="forbid")`.

    from commissure.api import Stage

    from .analyze import consolidate
    from .extract import extract
    from .schema import Consolidated, Report

    STAGES = [
        Stage("extract", extract, schema=Report, text="default"),
        Stage("consolidate", consolidate, schema=Consolidated, subject="group",
              needs=("extract",), group_by=lambda item: item.upstream["extract"].workstream),
    ]

Each run of a stage recomputes its scope with the current code (a snapshot). A run
interrupted or cut short by --limit resumes on the next invocation only if every file
in this folder and the stage's params are unchanged.

To mount a local web interface (`commissure serve`), define `INTERFACE = Interface(...)`
(`commissure.api`). Core provides the sticky header, live workspace status, pipeline
steps drawer, notices, and default footer; `render` returns a body fragment using the
primitives indexed at the top of `commissure/deliver/static/report.css`. Drop optional
`recipe/brand/{header.html,footer.html,brand.css,logo.svg}` (or set
`Interface(..., footer="...")`) to brand it without overriding core typography or header chrome.
"""

STAGES: list = []  # an empty list fails at startup: fill it in before running
