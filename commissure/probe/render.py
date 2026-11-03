"""Human-readable probe output. `--json` carries everything; this is the summary view."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def render(report: dict[str, Any], console: Console) -> None:
    s = report["summary"]
    console.print(f"[bold]{escape(report['path'])}[/bold]")
    ext = Table("extension", "files", title="Files by extension", title_justify="left")
    for k, v in report["by_extension"].items():
        ext.add_row(k, str(v))
    console.print(ext)
    ignored_note = f", {s['ignored']} ignored" if s.get("ignored") else ""
    console.print(
        f"{s['files']} files{ignored_note}: {s['inspected']} inspected,"
        f" {s['not_inspected']} not inspected,"
        f" {s['errors']} errors. PDF pages: {s['pdf_pages']} ({s['readable_pages']} readable,"
        f" {s['unreadable_pages']} unreadable, {s['blank_pages']} blank)"
    )
    for entry in report["files"]:
        console.print()
        console.print(f"[bold]{escape(entry['locator'])}[/bold] ({entry['status']})")
        if entry["status"] == "error":
            console.print(f"  [red]{escape(entry['error'])}[/red]")
        elif "pdf" in entry:
            _pdf(entry["pdf"], console)
        elif "docx" in entry:
            _docx(entry["docx"], console)
        elif "xlsx" in entry:
            for sheet in entry["xlsx"]["sheets"]:
                _sheet(f"sheet {escape(repr(sheet['name']))} ({sheet['state']})", sheet, console)
        elif "csv" in entry:
            c = entry["csv"]
            _sheet(f"encoding {c['encoding']}, delimiter {c['delimiter']!r}", c, console)


def _pdf(pdf: dict[str, Any], console: Console) -> None:
    console.print(
        f"  {pdf['pages']} pages: {pdf['readable']} readable, {pdf['unreadable']} unreadable,"
        f" {pdf['blank']} blank"
    )
    notable = [p for p in pdf["page_details"] if p["verdict"] != "readable" or p["flags"]]
    if notable:
        t = Table("page", "verdict", "reasons / flags", "glyphs", "undecodable", "image")
        for p in notable:
            t.add_row(
                str(p["page"]),
                p["verdict"],
                ", ".join(p["reasons"] + p["flags"]) or "-",
                str(p["chars"]),
                str(p["undecodable_chars"]),
                _pct(p["image_coverage"]),
            )
        console.print(t)
    unmapped = [f["name"] for f in pdf["fonts"] if not f["unicode_mapped"]]
    if unmapped:
        console.print(f"  fonts without unicode mapping: {escape(', '.join(unmapped))}")


def _docx(d: dict[str, Any], console: Console) -> None:
    console.print(
        f"  {d['paragraphs']} body paragraphs, {len(d['headings'])} headings,"
        f" {len(d['tables'])} tables"
    )
    for h in d["headings"]:
        console.print(f"  {'  ' * h['level']}H{h['level']} {escape(h['text'])}")
    for t in d["tables"]:
        console.print(
            f"  table {t['index']}: {t['rows']}x{t['cols']}, {t['merged_regions']} merged,"
            f" under {escape(repr(t['under_heading']))}"
        )
    for skip in d["flags"]["heading_level_skips"]:
        console.print(
            f"  [yellow]level skip H{skip['from']} -> H{skip['to']}:[/yellow] "
            f"{escape(skip['heading'])}"
        )
    for text in d["flags"]["possible_unstyled_headings"]:
        console.print(f"  [yellow]bold paragraph, not a heading style:[/yellow] {escape(text)}")


def _sheet(title: str, s: dict[str, Any], console: Console) -> None:
    console.print(
        f"  {title}: {s['data_rows']} data rows, header row {s['header_row']}"
        f" ({s.get('header_assumption', '-')}), {s['empty_rows']} empty rows"
    )
    if s["flags"]:
        console.print(f"  [yellow]flags: {', '.join(s['flags'])}[/yellow]")
    t = Table("col", "header", "types", "null rate")
    for c in s["columns"]:
        types = ", ".join(f"{k} {v}" for k, v in c["types"].items()) or "-"
        t.add_row(c["letter"], escape(str(c["header"])), types, _pct(c["null_rate"]))
    console.print(t)
