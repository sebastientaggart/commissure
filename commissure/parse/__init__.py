"""Parser registry for the fast tier.

A parser's version is computed, not hand-bumped: the versions of the libraries it
uses plus a hash of every source file its output depends on (the parser module, the
shared model, and the probe code it reuses, including the readability gate). Editing
any of those files changes the version, so the next parse run re-extracts; nobody
has to remember to bump a string (Records 2). A parser that drives a local tool (`ocr`
and Tesseract) adds that tool's version too, and `check()` says at startup whether the
tool is installed. Only the default parsers run without `--parser`; `ocr` never does.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import inspect
import platform
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from commissure.limits import DEFAULT_LIMITS, Limits
from commissure.parse.model import ParsedDoc
from commissure.parse.ocr import ParserUnavailable as ParserUnavailable
from commissure.parse.ocr import parse_ocr, tesseract_version
from commissure.parse.office import parse_docx
from commissure.parse.pdf import parse_pdfium, parse_pdfplumber
from commissure.parse.tabular import parse_csv, parse_json, parse_xlsx

MODEL = "commissure.parse.model"


@dataclass(frozen=True)
class Parser:
    name: str
    extensions: frozenset[str]
    fn: Callable[..., ParsedDoc]
    modules: tuple[str, ...]
    libraries: tuple[str, ...] = ()
    stdlib: bool = False  # output depends on the Python version itself
    external: Callable[[], str] | None = None  # a local tool's version, or ParserUnavailable

    def check(self) -> None:
        """Raise `ParserUnavailable` when a local tool this parser needs is missing."""
        if self.external is not None:
            self.external()

    @cached_property
    def _accepts_limits(self) -> bool:
        params = inspect.signature(self.fn).parameters.values()
        return (
            any(
                p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                for p in params
            )
            or len(params) >= 2
        )

    @cached_property
    def version(self) -> str:
        parts = [f"{lib}={importlib.metadata.version(lib)}" for lib in self.libraries]
        if self.stdlib:
            parts.append(f"python={platform.python_version()}")
        if self.external is not None:
            parts.append(self.external())
        h = hashlib.sha256()
        for module in sorted(self.modules):
            h.update(Path(importlib.import_module(module).__file__).read_bytes())
        parts.append(f"src={h.hexdigest()[:12]}")
        return ";".join(parts)

    def parse(self, path: Path, limits: Limits = DEFAULT_LIMITS) -> ParsedDoc:
        if self._accepts_limits:
            return self.fn(path, limits)
        return self.fn(path)


_PDF = (MODEL, "commissure.parse.pdf", "commissure.probe.pdf")
_TABULAR = (MODEL, "commissure.parse.tabular", "commissure.probe.tabular")

PARSERS = {
    p.name: p
    for p in (
        Parser(
            "pdfplumber",
            frozenset({".pdf"}),
            parse_pdfplumber,
            _PDF,
            ("pdfplumber", "pdfminer.six"),
        ),
        Parser(
            "pdfium",
            frozenset({".pdf"}),
            parse_pdfium,
            _PDF,
            ("pdfplumber", "pdfminer.six", "pypdfium2"),
        ),
        Parser(
            "ocr",
            frozenset({".pdf"}),
            parse_ocr,
            (*_PDF, "commissure.parse.ocr"),
            ("pdfplumber", "pdfminer.six", "pypdfium2", "pytesseract"),
            external=tesseract_version,
        ),
        Parser(
            "docx",
            frozenset({".docx"}),
            parse_docx,
            (MODEL, "commissure.parse.office", "commissure.probe.office"),
            ("python-docx",),
        ),
        Parser("xlsx", frozenset({".xlsx", ".xlsm"}), parse_xlsx, _TABULAR, ("openpyxl",)),
        Parser("csv", frozenset({".csv"}), parse_csv, _TABULAR, stdlib=True),
        Parser("json", frozenset({".json"}), parse_json, _TABULAR, stdlib=True),
    )
}

DEFAULT_FOR = {
    ".pdf": "pdfplumber",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".csv": "csv",
    ".json": "json",
}


DEFAULT_PARSERS = tuple(dict.fromkeys(DEFAULT_FOR.values()))
BUILTIN_PARSERS = dict(PARSERS)


def extension(locator: str) -> str:
    return Path(locator).suffix.lower()
