"""Repository tooling (`commissure/tools/`): the third-party license register and report.

`licenses.py` and its register `licenses.toml`, mounted as `commissure licenses`. Never
imported by recipes (`commissure/api.py`) or by runtime core modules, never touches
`state.db`, and never names a client, corpus, or business document type. Reusable stage
helpers go in `commissure/patterns/`; a solution's pre-ingest fetchers go in its
`recipe/tools/`.
"""
