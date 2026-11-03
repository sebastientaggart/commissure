"""Global resource limits across discovery, ingestion, probing, and parsing.

Source-wide limits (`max_source_files`, `max_source_bytes`) fail a run fast before blobs
are hashed or copied, catching mispointed directories immediately. Per-item limits
quarantine only the offending item with a specific machine-readable `reason_code` and
`{actual, limit}` diagnostics — never silently truncating content as `valid`.

Precedence (lowest to highest, with the winning source recorded in `runs.config["limits"]`):
    1. Core conservative defaults (`DEFAULT_LIMITS`)
    2. Optional `LIMITS = Limits(...)` declared in `recipe.py`
    3. `COMMISSURE_MAX_*` environment variables (empty or non-positive values fail at startup)
"""

from __future__ import annotations

import json
import math
import os
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

_INT_FIELDS = (
    "max_source_files",
    "max_source_bytes",
    "max_file_bytes",
    "max_archive_bytes",
    "max_pdf_pages",
    "max_ocr_pages",
    "max_sheets",
    "max_table_rows",
    "max_table_cells",
    "max_extracted_bytes",
)
_FLOAT_FIELDS = (
    "max_parse_seconds",
    "max_ocr_seconds",
)


@dataclass(frozen=True)
class Limits:
    """Upper bounds on source discovery, raw item size, document structure, and parse time."""

    max_source_files: int = 50_000
    max_source_bytes: int = 25 * 1024**3  # 25 GiB
    max_file_bytes: int = 100 * 1024**2  # 100 MiB
    max_archive_bytes: int = 512 * 1024**2  # 512 MiB uncompressed ZIP/OOXML
    max_pdf_pages: int = 2_500
    max_ocr_pages: int = 250
    max_sheets: int = 200
    max_table_rows: int = 200_000
    max_table_cells: int = 5_000_000
    max_extracted_bytes: int = 64 * 1024**2  # 64 MiB (text + structure UTF-8 bytes)
    max_parse_seconds: float = 180.0
    max_ocr_seconds: float = 900.0

    def __post_init__(self) -> None:
        for name in _INT_FIELDS:
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, int) or val < 1:
                raise ValueError(f"{name} must be a positive integer, got {val!r}")
        for name in _FLOAT_FIELDS:
            val = getattr(self, name)
            if (
                isinstance(val, bool)
                or not isinstance(val, int | float)
                or not math.isfinite(val)
                or val <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number, got {val!r}")
            object.__setattr__(self, name, float(val))


DEFAULT_LIMITS = Limits()

ENV_VARS: dict[str, str] = {f.name: f"COMMISSURE_{f.name.upper()}" for f in fields(Limits)}

LIMIT_REASON_FIELD: dict[str, str] = {
    "file_too_large": "max_file_bytes",
    "archive_too_large": "max_archive_bytes",
    "too_many_pages": "max_pdf_pages",
    "too_many_ocr_pages": "max_ocr_pages",
    "too_many_sheets": "max_sheets",
    "too_many_rows": "max_table_rows",
    "too_many_cells": "max_table_cells",
    "extracted_output_too_large": "max_extracted_bytes",
    "parse_timeout": "max_parse_seconds",
}


class LimitExceeded(Exception):
    """Raised when a source or item exceeds a configured `Limits` bound."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        actual: int | float,
        limit: int | float,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.actual = actual
        self.limit = limit
        self.detail: dict[str, Any] = {"actual": actual, "limit": limit, **extra}


def resolve_limits(
    recipe_limits: Limits | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Limits, dict[str, dict[str, Any]]]:
    """Resolve effective `Limits` and a provenance map (`value` and `from`) for `runs.config`."""
    if recipe_limits is not None and not isinstance(recipe_limits, Limits):
        raise ValueError(f"recipe LIMITS must be a Limits instance, got {recipe_limits!r}")
    environ = os.environ if env is None else env
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for f in fields(Limits):
        name = f.name
        default_val = getattr(DEFAULT_LIMITS, name)
        val = default_val
        src = "default"
        if recipe_limits is not None and getattr(recipe_limits, name) != default_val:
            val = getattr(recipe_limits, name)
            src = "recipe"
        env_key = ENV_VARS[name]
        if env_key in environ:
            raw = environ[env_key].strip()
            if not raw:
                raise ValueError(f"{env_key} must not be empty")
            try:
                val = int(raw) if name in _INT_FIELDS else float(raw)
            except ValueError as exc:
                raise ValueError(f"invalid {env_key}={environ[env_key]!r}: {exc}") from None
            src = "env"
        values[name] = val
        sources[name] = src
    resolved = Limits(**values)
    provenance = {
        f.name: {"value": getattr(resolved, f.name), "from": sources[f.name]}
        for f in fields(Limits)
    }
    return resolved, provenance


def check_file_bytes(path: Path, limits: Limits, *, size_bytes: int | None = None) -> int:
    """Raise `LimitExceeded('file_too_large', ...)` if `path` exceeds `limits.max_file_bytes`."""
    sz = path.stat().st_size if size_bytes is None else size_bytes
    if sz > limits.max_file_bytes:
        raise LimitExceeded(
            "file_too_large",
            f"file size {sz} bytes exceeds max_file_bytes ({limits.max_file_bytes})",
            actual=sz,
            limit=limits.max_file_bytes,
        )
    return sz


def check_archive_bytes(path: Path, limits: Limits) -> int:
    """Check total uncompressed bytes declared in a ZIP/OOXML container before unpacking."""
    total = 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            total += max(0, int(info.file_size))
            if total > limits.max_archive_bytes:
                raise LimitExceeded(
                    "archive_too_large",
                    f"archive uncompressed size exceeds max_archive_bytes"
                    f" ({limits.max_archive_bytes})",
                    actual=total,
                    limit=limits.max_archive_bytes,
                )
    return total


def is_limit_quarantine_stale(
    stage: str,
    outcome: str,
    reason_code: str | None,
    diagnostics_json: str | None,
    limits: Limits | None,
) -> bool:
    """True when an item was quarantined by a limit that has since been raised."""
    if limits is None or outcome != "quarantined" or not reason_code:
        return False
    field_name = (
        "max_ocr_seconds"
        if (reason_code == "parse_timeout" and stage == "parse.ocr")
        else LIMIT_REASON_FIELD.get(reason_code)
    )
    if field_name is None:
        return False
    current_limit = getattr(limits, field_name)
    if not diagnostics_json:
        return False
    try:
        diags = json.loads(diagnostics_json)
    except ValueError:
        return False
    if not isinstance(diags, list):
        return False
    for entry in diags:
        if not isinstance(entry, dict) or entry.get("code") != reason_code:
            continue
        detail = entry.get("detail")
        if isinstance(detail, dict):
            old_limit = detail.get("limit")
            if isinstance(old_limit, int | float) and not isinstance(old_limit, bool):
                return current_limit > old_limit
    return False
