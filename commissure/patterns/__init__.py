"""Graduated reusable helpers.

A helper moves here from a recipe when a second recipe needs it or the operator
asks for it (the Rule of Two in AGENTS.md). Recipes import from this package
directly; helpers are not re-exported from ``commissure.api``.
"""

# Core resolves evidence pages with it, so it lives in core; re-exported for recipes.
from commissure.locate import BlockLocation, locate_offset
from commissure.patterns.table import Column, WrittenTable, write_record_table

__all__ = [
    "BlockLocation",
    "Column",
    "WrittenTable",
    "locate_offset",
    "write_record_table",
]
