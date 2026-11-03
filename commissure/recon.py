"""Read-only measurements over a parsed workspace.

Opens `state.db` with `mode=ro` and `query_only`, the same way inspect does: no
lock, no migrate, no create. Reports folder clusters, repeated lines, identical
text hashes, table first-row signatures, and unreadable or OCR pages. It does
not label what a repeated line or a table is; the recipe judges.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from commissure.inspect import open_readonly
from commissure.parse import extension
from commissure.probe.tabular import HEADER_ASSUMPTION, probe_csv, probe_xlsx
from commissure.recipes import parsers_for
from commissure.runner import canonical
from commissure.workspace import BLOBS_DIR, DB_NAME, MIGRATIONS, BlobStore, WorkspaceError

DEFAULT_TEXT = ("ocr", "default")
PROBE_SHEET_EXTS = {".csv", ".xlsx", ".xlsm"}


def recon_workspace(
    root: Path,
    *,
    preference: tuple[str, ...] = DEFAULT_TEXT,
    min_repeat: int = 2,
) -> dict[str, Any]:
    """Measure one workspace. `preference` is the `--text` tuple; default matches stages."""
    if min_repeat < 1:
        raise WorkspaceError("--min-repeat must be at least 1")
    root = root.resolve()
    if not root.is_dir():
        raise WorkspaceError(f"workspace path {root} is not a directory")
    db_path = root / DB_NAME
    if not db_path.is_file():
        raise WorkspaceError(f"{root} has no {DB_NAME}")
    conn = open_readonly(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != len(MIGRATIONS):
            raise WorkspaceError(
                f"{root}: schema version {version}, this code expects {len(MIGRATIONS)}; "
                "recon does not migrate"
            )
        return _report(conn, root, preference=preference, min_repeat=min_repeat, version=version)
    except sqlite3.Error as exc:
        raise WorkspaceError(f"{root}: {exc}") from exc
    finally:
        conn.close()


def render(report: dict[str, Any], console: Console) -> None:
    """Tables for a person. `--json` is the full report."""
    console.print(f"[bold]{escape(report['workspace'])}[/bold]  schema {report['schema_version']}")
    s = report["summary"]
    console.print(
        f"{s['items']} items, {s['summarized']} summarized, {s['unparsed']} unparsed, "
        f"{s['errors']} errors; {s['identical_sets']} identical sets, "
        f"{s['repeated_lines']} repeated lines, {s['table_signatures']} table signatures; "
        f"{s['unreadable_pages']} unreadable pages, {s['ocr_pages']} ocr pages, "
        f"{s['ocr_not_run']} ocr not run"
    )
    console.print(
        f"preference: {', '.join(report['preference'])}  min-repeat: {report['min_repeat']}"
    )
    _table(
        console,
        "Locator parents",
        ["source", "parent", "items", "unparsed", "errors", "extensions"],
        [
            [
                row["source_id"],
                row["parent"],
                row["items"],
                row["unparsed"],
                row["errors"],
                ", ".join(f"{ext}={n}" for ext, n in row["extensions"].items()),
            ]
            for row in report["locator_parents"]
        ],
    )
    _table(
        console,
        "Repeated lines",
        ["files", "pages", "occurrences", "line"],
        [
            [row["files"], row["pages"], row["occurrences"], row["line"]]
            for row in report["repeated_lines"]
        ],
    )
    _table(
        console,
        "Identical texts",
        ["size", "parser", "parser_version", "text_hash", "locators"],
        [
            [
                row["size"],
                row["parser"],
                row["parser_version"],
                row["text_hash"][:12] + "…",
                ", ".join(row["locators"]),
            ]
            for row in report["identical"]
        ],
    )
    if report["near_duplicates"]["method"] is None:
        console.print("near-duplicates: not computed")
    _table(
        console,
        "Table signatures",
        ["tables", "columns", "first_row", "locators"],
        [
            [
                row["tables"],
                row["column_count"],
                canonical(row["first_row"]),
                ", ".join(sorted({e["locator"] for e in row["locators"]})),
            ]
            for row in report["table_signatures"]
        ],
    )
    _table(
        console,
        "Pages",
        ["locator", "page", "status", "ocr", "confidence", "reasons", "flags"],
        [
            [
                row["locator"],
                row["page"],
                row["status"],
                row.get("ocr"),
                row.get("confidence"),
                ",".join(row.get("reasons") or []),
                ",".join(row.get("flags") or []),
            ]
            for row in report["pages"]
        ],
    )
    _table(
        console,
        "Errors",
        ["locator", "message"],
        [[row["locator"], row["message"]] for row in report["errors"]],
    )


def _table(console: Console, title: str, headers: list[str], rows: list[list[Any]]) -> None:
    table = Table(*headers, title=f"{title} ({len(rows)})", title_justify="left")
    for row in rows:
        table.add_row(*(_cell(value) for value in row))
    console.print(table)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return escape(str(value))


def _report(
    conn: sqlite3.Connection,
    root: Path,
    *,
    preference: tuple[str, ...],
    min_repeat: int,
    version: int,
) -> dict[str, Any]:
    items_rows = conn.execute(
        "SELECT item_id, source_id, locator, role, item_version_id, bytes_hash"
        " FROM current_items ORDER BY source_id, locator"
    ).fetchall()
    text_by_version: dict[int, dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in conn.execute(
        "SELECT item_version_id, extracted_text_id, parser, parser_version, text_hash,"
        " text, structure, stale FROM current_text"
    ):
        text_by_version[row["item_version_id"]][row["parser"]] = row

    outcomes_by_item: dict[int, dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in conn.execute(
        "SELECT subject_key, stage, outcome, reason_code, diagnostics, extracted_text_id"
        " FROM current_outcomes WHERE stage = 'parse' OR stage LIKE 'parse.%'"
    ):
        key = row["subject_key"]
        if not key.startswith("item:"):
            continue
        item_id = int(key[5:])
        outcomes_by_item[item_id][row["stage"]] = row

    blobs = BlobStore(root / BLOBS_DIR)
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []

    for r in items_rows:
        item = _item_entry(
            r,
            text_by_version.get(r["item_version_id"], {}),
            outcomes_by_item.get(r["item_id"], {}),
            preference=preference,
        )
        items.append(item)
        if item["status"] == "error":
            errors.append({"locator": item["locator"], "message": item["error"]})
        if item.get("_selected") is not None:
            selected.append(item["_selected"])
        # drop private keys from the public item
        item.pop("_selected", None)
        item.pop("error", None)

    locator_parents = _locator_parents(items)
    repeated_lines = _repeated_lines(selected, min_repeat=min_repeat)
    identical = _identical(selected)
    table_signatures = _table_signatures(selected)
    # Probe fallback for unparsed tabular items with no selected row
    for r, item in zip(items_rows, items, strict=True):
        if item["status"] != "unparsed":
            continue
        if extension(r["locator"]) not in PROBE_SHEET_EXTS:
            continue
        try:
            sigs = _probe_sheet_signatures(r, blobs)
        except Exception as exc:
            item["status"] = "error"
            msg = f"{type(exc).__name__}: {exc}"
            item["parse_reason"] = "probe_error"
            errors.append({"locator": r["locator"], "message": msg})
            continue
        table_signatures.extend(sigs)
    table_signatures = _merge_table_signatures(table_signatures)

    pages = _pages(items_rows, items, text_by_version, outcomes_by_item)

    summary = {
        "items": len(items),
        "summarized": sum(1 for i in items if i["status"] == "summarized"),
        "unparsed": sum(1 for i in items if i["status"] == "unparsed"),
        "errors": len(errors),
        "identical_sets": len(identical),
        "repeated_lines": len(repeated_lines),
        "table_signatures": len(table_signatures),
        "unreadable_pages": sum(1 for p in pages if p["status"] == "unreadable"),
        "ocr_pages": sum(1 for p in pages if p.get("ocr") == "recognised"),
        "ocr_not_run": sum(1 for p in pages if p.get("ocr") == "not_run"),
    }
    return {
        "workspace": str(root),
        "schema_version": version,
        "preference": list(preference),
        "min_repeat": min_repeat,
        "summary": summary,
        "items": items,
        "locator_parents": locator_parents,
        "repeated_lines": repeated_lines,
        "identical": identical,
        # Equality only: a similarity threshold needs a corpus measurement this command
        # does not have, and judging near-duplicates belongs to the recipe.
        "near_duplicates": {"method": None, "threshold": None, "sets": []},
        "table_signatures": table_signatures,
        "pages": pages,
        "errors": errors,
    }


def _item_entry(
    r: sqlite3.Row,
    by_parser: dict[str, sqlite3.Row],
    outcomes: dict[str, sqlite3.Row],
    *,
    preference: tuple[str, ...],
) -> dict[str, Any]:
    locator = r["locator"]
    item: dict[str, Any] = {
        "source_id": r["source_id"],
        "locator": locator,
        "role": r["role"],
        "status": "unparsed",
        "parser": None,
        "parser_version": None,
        "text_hash": None,
        "stale": None,
        "parse_outcome": None,
        "parse_reason": None,
        "_selected": None,
    }

    # parse_error wins as error even when somehow a text row exists
    for _stage, out in outcomes.items():
        if (
            out["outcome"] == "quarantined"
            and out["reason_code"]
            and (
                out["reason_code"] == "parse_error" or out["reason_code"].startswith("parse_error")
            )
        ):
            item["status"] = "error"
            item["parse_outcome"] = out["outcome"]
            item["parse_reason"] = out["reason_code"]
            item["error"] = _parse_error_message(out)
            return item

    preferred = parsers_for(preference, locator)
    found = [by_parser[name] for name in preferred if name in by_parser]
    text_row = next((row for row in found if not row["stale"]), found[0] if found else None)
    if text_row is None:
        # no_parser skip or quarantined with no text
        skip = outcomes.get("parse")
        if skip is not None and skip["reason_code"] == "no_parser":
            item["parse_outcome"] = skip["outcome"]
            item["parse_reason"] = skip["reason_code"]
        else:
            # any parse.* quarantine / skip without a row
            for stage, out in outcomes.items():
                if stage.startswith("parse."):
                    item["parse_outcome"] = out["outcome"]
                    item["parse_reason"] = out["reason_code"]
                    break
        return item

    try:
        structure = json.loads(text_row["structure"]) if text_row["structure"] else {}
        if not isinstance(structure, dict):
            raise ValueError("structure is not an object")
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        item["status"] = "error"
        item["parser"] = text_row["parser"]
        item["parser_version"] = text_row["parser_version"]
        item["text_hash"] = text_row["text_hash"]
        item["stale"] = bool(text_row["stale"])
        item["error"] = f"invalid structure: {exc}"
        item["parse_reason"] = "invalid_structure"
        return item

    stage = f"parse.{text_row['parser']}"
    out = outcomes.get(stage)
    item.update(
        {
            "status": "summarized",
            "parser": text_row["parser"],
            "parser_version": text_row["parser_version"],
            "text_hash": text_row["text_hash"],
            "stale": bool(text_row["stale"]),
            "parse_outcome": out["outcome"] if out else None,
            "parse_reason": out["reason_code"] if out else None,
            "_selected": {
                "locator": locator,
                "parser": text_row["parser"],
                "parser_version": text_row["parser_version"],
                "text_hash": text_row["text_hash"],
                "text": text_row["text"] or "",
                "structure": structure,
                "item_version_id": r["item_version_id"],
                "bytes_hash": r["bytes_hash"],
            },
        }
    )
    return item


def _parse_error_message(out: sqlite3.Row) -> str:
    try:
        diags = json.loads(out["diagnostics"] or "[]")
    except json.JSONDecodeError:
        return out["reason_code"] or "parse_error"
    for d in diags:
        if d.get("code") == "parse_error":
            return d.get("message") or "parse_error"
    return out["reason_code"] or "parse_error"


def _locator_parents(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, str], dict[str, Any]] = {}
    for item in items:
        parent = PurePosixPath(item["locator"]).parent.as_posix()
        key = (item["source_id"], parent)
        g = groups.get(key)
        if g is None:
            g = {
                "source_id": item["source_id"],
                "parent": parent,
                "items": 0,
                "extensions": Counter(),
                "locators": [],
                "unparsed": 0,
                "errors": 0,
            }
            groups[key] = g
        g["items"] += 1
        g["extensions"][extension(item["locator"]) or "(none)"] += 1
        g["locators"].append(item["locator"])
        if item["status"] == "unparsed":
            g["unparsed"] += 1
        elif item["status"] == "error":
            g["errors"] += 1
    out = []
    for g in sorted(groups.values(), key=lambda x: (x["source_id"], x["parent"])):
        out.append(
            {
                **g,
                "extensions": dict(sorted(g["extensions"].items())),
                "locators": sorted(g["locators"]),
            }
        )
    return out


def _line_pages(text: str, structure: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per stripped non-empty line: count and distinct page numbers (when page blocks exist)."""
    page_blocks = [b for b in structure.get("blocks") or () if b.get("kind") == "page"]
    stats: dict[str, dict[str, Any]] = {}
    if page_blocks:
        for block in page_blocks:
            start = block.get("start", 0)
            end = block.get("end", start)
            page = block.get("page")
            for raw in text[start:end].splitlines():
                line = raw.strip()
                if not line:
                    continue
                entry = stats.setdefault(line, {"count": 0, "pages": set()})
                entry["count"] += 1
                if page is not None:
                    entry["pages"].add(page)
        return stats
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        entry = stats.setdefault(line, {"count": 0, "pages": set()})
        entry["count"] += 1
    return stats


def _repeated_lines(selected: list[dict[str, Any]], *, min_repeat: int) -> list[dict[str, Any]]:
    # line -> list of per-file stats
    by_line: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sel in selected:
        stats = _line_pages(sel["text"], sel["structure"])
        has_pages = any(b.get("kind") == "page" for b in sel["structure"].get("blocks") or ())
        for line, info in stats.items():
            by_line[line].append(
                {
                    "locator": sel["locator"],
                    "parser": sel["parser"],
                    "count": info["count"],
                    "pages": sorted(info["pages"]) if has_pages else None,
                    "page_count": len(info["pages"]) if has_pages else 0,
                    "has_pages": has_pages,
                }
            )

    out = []
    for line, files in by_line.items():
        qualify = len(files) >= min_repeat or any(
            (f["has_pages"] and f["page_count"] >= min_repeat)
            or (not f["has_pages"] and f["count"] >= min_repeat)
            for f in files
        )
        if not qualify:
            continue
        any_pages = any(f["has_pages"] for f in files)
        distinct_pages = set()
        if any_pages:
            for f in files:
                if f["pages"]:
                    distinct_pages.update(f["pages"])
        out.append(
            {
                "line": line,
                "occurrences": sum(f["count"] for f in files),
                "pages": len(distinct_pages) if any_pages else None,
                "files": len(files),
                "locators": [
                    {
                        "locator": f["locator"],
                        "parser": f["parser"],
                        "pages": f["pages"],
                        "count": f["count"],
                    }
                    for f in sorted(files, key=lambda x: x["locator"])
                ],
            }
        )
    out.sort(
        key=lambda e: (
            -e["files"],
            -(e["pages"] if e["pages"] is not None else -1),
            e["line"],
        )
    )
    return out


def _identical(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for sel in selected:
        key = (sel["parser"], sel["parser_version"], sel["text_hash"])
        groups[key].append(sel["locator"])
    out = []
    for (parser, parser_version, text_hash), locators in groups.items():
        if len(locators) < 2:
            continue
        out.append(
            {
                "parser": parser,
                "parser_version": parser_version,
                "text_hash": text_hash,
                "size": len(locators),
                "locators": sorted(locators),
            }
        )
    out.sort(key=lambda e: (-e["size"], e["parser"], e["text_hash"]))
    return out


def _table_signatures(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flat list of per-table entries; merge later."""
    entries = []
    for sel in selected:
        tables = sel["structure"].get("tables") or []
        for index, table in enumerate(tables):
            rows = table.get("rows") or []
            first = list(rows[0]) if rows else []
            entries.append(
                {
                    "first_row": first,
                    "locator": sel["locator"],
                    "parser": sel["parser"],
                    "source": "parsed_table",
                    "page": table.get("page"),
                    "sheet": table.get("sheet"),
                    "table": index,
                    "rows": len(rows),
                    "heading_path": table.get("heading_path"),
                }
            )
    return entries


def _probe_sheet_signatures(r: sqlite3.Row, blobs: BlobStore) -> list[dict[str, Any]]:
    digest = r["bytes_hash"]
    if not digest or not blobs.has(digest):
        raise FileNotFoundError(f"no blob {digest} for {r['locator']}")
    src = blobs.path_for(digest)
    suffix = extension(r["locator"])
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(src.read_bytes())
    try:
        if suffix == ".csv":
            probed = probe_csv(tmp_path, sample_rows=0)
            sheets = [
                {
                    "name": None,
                    "columns": probed.get("columns") or [],
                    "data_rows": probed.get("data_rows", 0),
                    "header_row": probed.get("header_row"),
                }
            ]
        else:
            probed = probe_xlsx(tmp_path, sample_rows=0)
            sheets = probed.get("sheets") or []
    finally:
        tmp_path.unlink(missing_ok=True)

    entries = []
    for index, sheet in enumerate(sheets):
        columns = sheet.get("columns") or []
        first = [c.get("header") for c in columns]
        entries.append(
            {
                "first_row": first,
                "locator": r["locator"],
                "parser": None,
                "source": "probe_sheet",
                "page": None,
                "sheet": sheet.get("name"),
                "table": index,
                "rows": (sheet.get("data_rows") or 0)
                + (1 if sheet.get("header_row") is not None else 0),
                "heading_path": None,
                "assumption": HEADER_ASSUMPTION,
            }
        )
    return entries


def _merge_table_signatures(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in entries:
        key = canonical(e["first_row"])
        if key not in groups:
            groups[key] = {
                "first_row": e["first_row"],
                "column_count": len(e["first_row"]),
                "tables": 0,
                "locators": [],
            }
            order.append(key)
        g = groups[key]
        g["tables"] += 1
        loc = {
            "locator": e["locator"],
            "parser": e["parser"],
            "source": e["source"],
            "page": e["page"],
            "sheet": e["sheet"],
            "table": e["table"],
            "rows": e["rows"],
            "heading_path": e["heading_path"],
        }
        if e.get("assumption") is not None:
            loc["assumption"] = e["assumption"]
        g["locators"].append(loc)
    out = [groups[k] for k in order]
    out.sort(key=lambda e: (-e["tables"], canonical(e["first_row"])))
    return out


def _pages(
    items_rows: list[sqlite3.Row],
    items: list[dict[str, Any]],
    text_by_version: dict[int, dict[str, sqlite3.Row]],
    outcomes_by_item: dict[int, dict[str, sqlite3.Row]],
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for r, item in zip(items_rows, items, strict=True):
        outcomes = outcomes_by_item.get(r["item_id"], {})
        ocr_outcome = outcomes.get("parse.ocr")
        by_parser = text_by_version.get(r["item_version_id"], {})

        # Prefer ocr structure for pages when an ocr row exists; else selected/default.
        page_row = None
        page_stale = False
        if "ocr" in by_parser:
            page_row = by_parser["ocr"]
            # stale relative to preference selection when selected is not ocr
            if item.get("parser") != "ocr":
                page_stale = True
        elif item["status"] == "summarized" and item["parser"] in by_parser:
            page_row = by_parser[item["parser"]]

        if page_row is not None:
            try:
                structure = json.loads(page_row["structure"] or "{}")
            except json.JSONDecodeError:
                continue
            for block in structure.get("blocks") or ():
                if block.get("kind") != "page":
                    continue
                status = block.get("status", "readable")
                flags = list(block.get("flags") or [])
                reasons = list(block.get("reasons") or [])
                is_ocr_flag = "ocr" in flags
                if status == "readable" and not is_ocr_flag:
                    continue
                if status not in ("unreadable", "blank") and not is_ocr_flag:
                    continue
                entry: dict[str, Any] = {
                    "locator": r["locator"],
                    "page": block.get("page"),
                    "status": status,
                    "reasons": reasons,
                    "flags": flags,
                    "confidence": block.get("confidence"),
                    "parser": page_row["parser"],
                    "parser_version": page_row["parser_version"],
                }
                if page_stale:
                    entry["stale"] = True
                if is_ocr_flag:
                    entry["ocr"] = "recognised"
                elif status == "blank":
                    entry["ocr"] = None
                elif status == "unreadable":
                    if ocr_outcome is not None or any(x.startswith("ocr_") for x in reasons):
                        entry["ocr"] = "unreadable"
                    else:
                        entry["ocr"] = "not_run"
                pages.append(entry)
            continue

        # No text row: quarantined with unreadable_pages diagnostics
        if item["status"] == "error":
            continue
        for stage, out in outcomes.items():
            if not stage.startswith("parse."):
                continue
            try:
                diags = json.loads(out["diagnostics"] or "[]")
            except json.JSONDecodeError:
                continue
            for d in diags:
                if d.get("code") != "unreadable_pages":
                    continue
                detail = d.get("detail") or {}
                reasons_map = detail.get("reasons") or {}
                for page_s, reason_list in sorted(
                    reasons_map.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0
                ):
                    entry = {
                        "locator": r["locator"],
                        "page": int(page_s) if str(page_s).isdigit() else page_s,
                        "status": "unreadable",
                        "reasons": list(reason_list),
                        "flags": [],
                        "confidence": None,
                        "parser": stage.removeprefix("parse."),
                        "parser_version": None,
                        "ocr": (
                            "unreadable"
                            if ocr_outcome is not None
                            or any(str(x).startswith("ocr_") for x in reason_list)
                            else "not_run"
                        ),
                    }
                    pages.append(entry)
    pages.sort(key=lambda p: (p["locator"], p["page"] if p["page"] is not None else -1))
    return pages
