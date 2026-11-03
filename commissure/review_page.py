"""The review page: the verdict log's interface inside `commissure serve` (`AGENTS.md`, Section 1).

Core-owned routes beside a recipe's `INTERFACE`, which `serve` requires, calling the same
functions as `commissure review` (`commissure/review.py`); no review logic lives here.

    GET  /review                      samples with progress and rates, a form to draw one
    GET  /review/sample/<id>          a sample's instances; bulk-confirm earlier verdicts
    GET  /review/instance/<id>        one instance beside its original, with answer buttons
    GET  /review/original/<version>   the exact ingested bytes of one item version
    GET  /review/tree/<version>       a JSON original as a collapsible tree (no scripts)
    POST /review/sample | /review/record | /review/carry   (CSRF-checked, no run mutex)

Originals are untrusted input: public comments are written by anyone. Every originals
response carries `Content-Security-Policy: sandbox`, so an original runs in an opaque
origin and cannot read the interface's CSRF token or post to it. Only inert types display
in the browser (PDF, raster images, plain text); everything else, HTML and SVG included,
is a download. Bytes come from the blob store (what was parsed, never the intake copy) and
are streamed, keyed by item version so only this workspace's items can be served.

Review pages render corpus text (quotes, claims, context). The offline guard scans each
page for external resource URLs, and autoescaping leaves `url(` and `@import` intact, so
a comment quoting CSS would make the page refuse to render. Every value these templates
print passes `_inert`, which also encodes `(` and `@`: the text reads the same and can no
longer match the guard.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from collections.abc import Mapping
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

from commissure import review
from commissure.inspect import open_readonly
from commissure.workspace import BLOBS_DIR, DB_NAME, MIGRATIONS, BlobStore, WorkspaceError

if TYPE_CHECKING:
    from commissure.serve import HttpResponse, InterfaceApp

_TEMPLATES_DIR = Path(__file__).resolve().parent / "deliver" / "templates"
REVIEWER_COOKIE = "commissure_reviewer"
DEFAULT_SAMPLE_N = 30

# Shown in the browser; every other type is a download. Text is served as text/plain.
INLINE_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    ".json": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
}
ORIGINAL_HEADERS = {
    "Content-Security-Policy": "sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",
    "Referrer-Policy": "no-referrer",
}


def _inert(value: Any) -> Any:
    """Escape a printed value, and encode `(` and `@` so corpus text cannot trip the guard."""
    if isinstance(value, Markup) or not isinstance(value, str):
        return value
    return Markup(str(escape(value)).replace("(", "&#40;").replace("@", "&#64;"))


def _pretty(value: Any) -> str:
    # A plain string (not `tojson`'s Markup), so `_inert` still applies to it.
    return json.dumps(value, indent=2, ensure_ascii=False)


_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True, finalize=_inert)
_ENV.filters["pretty"] = _pretty
_ENV.filters["pct"] = lambda v: "-" if v is None else f"{v:.1%}"
_ENV.filters["filename"] = lambda locator: PurePosixPath(locator).name


def _reviewer_default(headers: Mapping[str, str]) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("cookie", ""))
    except Exception:  # a malformed cookie header only loses the prefill
        pass
    else:
        morsel = cookie.get(REVIEWER_COOKIE)
        if morsel and (from_cookie := urllib.parse.unquote(morsel.value).strip()):
            return from_cookie
    return os.environ.get("COMMISSURE_REVIEWER", "").strip()


def _reviewer_cookie(reviewer: str) -> str:
    value = urllib.parse.quote(reviewer.strip(), safe="")
    return f"{REVIEWER_COOKIE}={value}; Path=/; SameSite=Strict; HttpOnly; Max-Age=31536000"


TREE_MAX_BYTES = 5 << 20  # larger JSON originals are offered as raw text only
TREE_OPEN_DEPTH = 2  # levels open by default; deeper ones open only on the path to a mark


def _marked(text: str, mark: str | None) -> str:
    if not mark or mark not in text:
        return str(_inert(text))
    return f"<mark>{_inert(mark)}</mark>".join(str(_inert(part)) for part in text.split(mark))


def render_json_tree(value: Any, mark: str | None = None) -> str:
    """JSON as nested `<details>`: collapsible with no script, so it works under the sandbox.

    Strings containing `mark` show it highlighted, and every node on the way to one is
    open. Every value passes `_inert`, like the review pages.
    """

    def node(value: Any, label: str | None, depth: int) -> tuple[str, bool]:
        key = f'<span class="jt-key">{_inert(label)}</span> ' if label is not None else ""
        if isinstance(value, dict | list):
            pairs = value.items() if isinstance(value, dict) else enumerate(value)
            kids = [node(v, str(k), depth + 1) for k, v in pairs]
            found = any(hit for _, hit in kids)
            ends = "{}" if isinstance(value, dict) else "[]"
            if not kids:
                return f'<div class="jt-leaf">{key}<span class="jt-meta">{ends}</span></div>', False
            opened = " open" if depth < TREE_OPEN_DEPTH or found else ""
            summary = f'{key}<span class="jt-meta">{ends[0]}{len(kids)}{ends[1]}</span>'
            body = "".join(html for html, _ in kids)
            return f"<details{opened}><summary>{summary}</summary>{body}</details>", found
        if isinstance(value, str):
            hit = bool(mark) and mark in value
            text = _marked(value, mark)
            return f'<div class="jt-leaf">{key}<span class="jt-str">{text}</span></div>', hit
        kind = "jt-null" if value is None else "jt-lit"
        literal = _inert(json.dumps(value))
        return f'<div class="jt-leaf">{key}<span class="{kind}">{literal}</span></div>', False

    return node(value, None, 0)[0]


_TREE_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<link rel="stylesheet" href="/static/report.css">
<style>
  body {{ margin: 0; padding: 0.75rem 1rem; background: var(--brand-sheet); color: var(--brand-ink);
         font: 0.85rem/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .jt-head {{ color: var(--brand-ink-soft); margin: 0 0 0.6rem; font-family: inherit; }}
  details {{ margin-left: 1.1rem; }}
  body > details {{ margin-left: 0; }}
  summary {{ cursor: pointer; margin-left: -1.1rem; }}
  .jt-leaf {{ margin-left: 1.1rem; white-space: pre-wrap; overflow-wrap: anywhere; }}
  .jt-key {{ color: var(--brand-primary); }}
  .jt-meta, .jt-null {{ color: var(--brand-ink-soft); }}
  .jt-str {{ color: var(--status-ok-fg); }}
  .jt-lit {{ color: var(--status-info-fg); }}
  mark {{ background: var(--status-warn-bg); color: var(--status-warn-fg); }}
</style></head>
<body><p class="jt-head">{name} &middot; {size} &middot;
<a href="/review/original/{version}">raw text</a></p>
{tree}
</body></html>
"""


class ReviewPages:
    """Request handling for `/review/*`, bound to one `InterfaceApp`."""

    def __init__(self, app: InterfaceApp) -> None:
        self.app = app
        self.root = app.workspace_root

    # --- plumbing ---------------------------------------------------------------------

    def _read(self) -> sqlite3.Connection | None:
        """A read-only connection, or `None` when there is no current-schema workspace yet."""
        db_path = self.root / DB_NAME
        if not db_path.is_file():
            return None
        conn = open_readonly(db_path)
        if conn.execute("PRAGMA user_version").fetchone()[0] != len(MIGRATIONS):
            conn.close()
            return None
        return conn

    def _page(self, template: str, context: dict[str, Any], headers: Mapping[str, str]):
        recipe = self.app._load_current_recipe()
        notice_token = context.pop("notice_token", None)
        body = Markup(_ENV.get_template(template).render(**context))
        return self.app._render_html(
            recipe,
            partial=False,
            notice=self.app._pop_notice(notice_token),
            body=body,
            review_mode=True,
        )

    def _redirect(self, location: str, *, notice=None, cookie: str | None = None) -> HttpResponse:
        from commissure.serve import HttpResponse

        if notice is not None:
            sep = "&" if "?" in location else "?"
            location += f"{sep}notice={urllib.parse.quote(self.app._store_notice(notice))}"
        headers = {"Location": location, "Cache-Control": "no-store"}
        if cookie:
            headers["Set-Cookie"] = cookie
        return HttpResponse(status=303, headers=headers, body=b"")

    @staticmethod
    def _id(segment: str) -> int | None:
        return int(segment) if segment.isdigit() else None

    # --- GET --------------------------------------------------------------------------

    def get(self, path: str, query: Mapping[str, list[str]], headers: Mapping[str, str]):
        """The response for a `/review` GET path, or `None` when the path is not one of ours."""
        notice = query.get("notice", [None])[-1]
        parts = [p for p in path.removeprefix("/review").split("/") if p]
        if not parts:
            return self._index(notice, headers)
        if len(parts) == 2 and (ident := self._id(parts[1])) is not None:
            if parts[0] == "sample":
                return self._sample(ident, notice, headers)
            if parts[0] == "instance":
                return self._instance(ident, notice, headers)
            if parts[0] == "original":
                return self._original(ident)
            if parts[0] == "tree":
                return self._tree(ident, query.get("mark", [None])[-1])
        return self.app._text_error(404, "not found")

    def _index(self, notice: str | None, headers: Mapping[str, str]):
        conn = self._read()
        context: dict[str, Any] = {
            "notice_token": notice,
            "ready": conn is not None,
            "csrf_token": self.app.csrf_token,
            "reviewer": _reviewer_default(headers),
            "default_n": DEFAULT_SAMPLE_N,
        }
        if conn is not None:
            try:
                recipe = self.app._load_current_recipe()
                rep = review.report(conn)
                for s in rep["samples"]:
                    s["next_instance"] = review.pending(conn, s["id"])
                context |= {
                    "checks": list(review.iter_checks_with_population(conn, recipe)),
                    "samples": rep["samples"],
                    "spot_checks": rep["spot_checks"],
                }
            finally:
                conn.close()
        return self._page("review_index.html.j2", context, headers)

    def _sample(self, sample_id: int, notice: str | None, headers: Mapping[str, str]):
        conn = self._read()
        if conn is None:
            return self.app._text_error(404, "no workspace to review yet")
        try:
            [summary] = [s for s in review.report(conn)["samples"] if s["id"] == sample_id] or [
                None
            ]
            if summary is None:
                return self.app._text_error(404, f"no sample {sample_id}")
            instances = review.sample_instances(conn, sample_id)
            next_instance = review.pending(conn, sample_id)
        finally:
            conn.close()
        carryable = [i for i in instances if i["answer"] is None and len(i["earlier_answers"]) == 1]
        return self._page(
            "review_sample.html.j2",
            {
                "notice_token": notice,
                "sample": summary,
                "instances": instances,
                "next_instance": next_instance,
                "carryable": len(carryable),
                "csrf_token": self.app.csrf_token,
                "reviewer": _reviewer_default(headers),
            },
            headers,
        )

    def _instance(self, instance_id: int, notice: str | None, headers: Mapping[str, str]):
        conn = self._read()
        if conn is None:
            return self.app._text_error(404, "no workspace to review yet")
        try:
            recipe = self.app._load_current_recipe()
            try:
                detail = review.describe(conn, self.root, instance_id, recipe)
            except review.ReviewError as exc:
                return self.app._text_error(404, str(exc))
        finally:
            conn.close()
        viewers = self._viewers(detail)
        return self._page(
            "review_instance.html.j2",
            {
                "notice_token": notice,
                "d": detail,
                "viewers": viewers,
                "csrf_token": self.app.csrf_token,
                "reviewer": _reviewer_default(headers),
            },
            headers,
        )

    @staticmethod
    def _viewers(detail: dict[str, Any]) -> list[dict[str, Any]]:
        """Originals worth opening beside the claim, evidence first, each at its page."""
        out: list[dict[str, Any]] = []
        seen: set[tuple[int, int | None]] = set()

        def add(
            version_id: int | None,
            locator: str,
            page: int | None,
            why: str,
            mark: str | None = None,
        ) -> None:
            if version_id is None or (version_id, page) in seen:
                return
            seen.add((version_id, page))
            ext = PurePosixPath(locator).suffix.lower()
            if ext == ".json":
                query = f"?mark={urllib.parse.quote(mark)}" if mark else ""
                out.append(
                    {
                        "url": f"/review/tree/{version_id}{query}",
                        "locator": locator,
                        "page": None,
                        "why": why,
                        "inline": True,
                        "view": "tree",
                    }
                )
            url = f"/review/original/{version_id}" + (f"#page={page}" if page else "")
            out.append(
                {
                    "url": url,
                    "locator": locator,
                    "page": page,
                    "why": why,
                    "inline": ext in INLINE_TYPES,
                    "view": "raw text" if ext == ".json" else None,
                }
            )

        for e in detail["evidence"]:
            add(e["item_version_id"], e["locator"], e["page"], "evidence", e["span_text"])
        for o in detail["originals"]:
            for p in o["pages"]:
                add(o["item_version_id"], o["locator"], p["page"], "flagged page")
            add(o["item_version_id"], o["locator"], None, "original")
        return out

    def _stored(self, version_id: int) -> tuple[str, Path] | None:
        """The locator and blob path of one item version of this workspace, or `None`."""
        conn = self._read()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT i.locator, v.bytes_hash FROM item_versions v"
                " JOIN items i ON i.id = v.item_id WHERE v.id = ?",
                (version_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["bytes_hash"] is None:
            return None
        return row["locator"], BlobStore(self.root / BLOBS_DIR).path_for(row["bytes_hash"])

    def _tree(self, version_id: int, mark: str | None) -> HttpResponse:
        """A JSON original as a collapsible tree, under the same sandbox as the raw bytes."""
        from commissure.serve import HttpResponse

        stored = self._stored(version_id)
        if stored is None or PurePosixPath(stored[0]).suffix.lower() != ".json":
            return self.app._text_error(404, "not found")
        locator, blob = stored
        if not blob.is_file():
            return self.app._text_error(404, "the stored bytes for this item are missing")
        size = blob.stat().st_size
        if size > TREE_MAX_BYTES:
            tree = f"<p>Too large for a tree ({size:,} bytes); open the raw text.</p>"
        else:
            try:
                tree = render_json_tree(json.loads(blob.read_bytes()), mark or None)
            except (ValueError, RecursionError) as exc:
                tree = f"<p>Not valid JSON ({_inert(str(exc))}); open the raw text.</p>"
        name = PurePosixPath(locator).name
        html = _TREE_PAGE.format(
            title=_inert(name), name=_inert(locator), size=f"{size:,} bytes",
            version=version_id, tree=tree,
        )  # fmt: skip
        headers = {**ORIGINAL_HEADERS, "Content-Type": "text/html; charset=utf-8"}
        return HttpResponse(status=200, headers=headers, body=html.encode("utf-8"))

    def _original(self, version_id: int) -> HttpResponse:
        from commissure.serve import HttpResponse

        stored = self._stored(version_id)
        if stored is None:
            return self.app._text_error(404, "not found")
        locator, blob = stored
        if not blob.is_file():
            return self.app._text_error(404, "the stored bytes for this item are missing")
        name = PurePosixPath(locator).name
        ext = PurePosixPath(name).suffix.lower()
        ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
        disposition = "inline" if ext in INLINE_TYPES else "attachment"
        headers = {
            **ORIGINAL_HEADERS,
            "Content-Type": INLINE_TYPES.get(ext, "application/octet-stream"),
            "Content-Disposition": f'{disposition}; filename="{ascii_name}";'
            f" filename*=UTF-8''{urllib.parse.quote(name)}",
        }
        return HttpResponse(status=200, headers=headers, body=b"", file=blob)

    # --- POST -------------------------------------------------------------------------

    def post(self, path: str, form: Mapping[str, str], headers: Mapping[str, str]):
        """The response for a CSRF-checked `/review` POST, or `None` when not one of ours."""
        from commissure.serve import Notice

        if path not in ("/review/sample", "/review/record", "/review/carry"):
            return None
        reviewer = form.get("reviewer", "").strip()
        cookie = _reviewer_cookie(reviewer) if reviewer else None
        try:
            conn = review.open_for_review(self.root)
        except WorkspaceError as exc:
            return self._redirect("/review", notice=Notice(str(exc), is_error=True))
        try:
            recipe = self.app._load_current_recipe()
            if path == "/review/sample":
                n = int(form.get("n", "") or 0)
                seed_raw = form.get("seed", "").strip()
                out = review.sample(
                    conn,
                    recipe,
                    form.get("check", ""),
                    n,
                    reviewer=reviewer,
                    seed=int(seed_raw) if seed_raw else None,
                )
                first = review.pending(conn, out["sample_id"])
                every = " (every instance)" if out["census"] else ""
                msg = (
                    f"Sample {out['sample_id']}: {out['n']} of {out['population']} instances"
                    f" of {out['check_id']}{every}, seed {out['seed']}."
                )
                return self._redirect(
                    f"/review/instance/{first}", notice=Notice(msg), cookie=cookie
                )
            if path == "/review/record":
                instance_id = int(form.get("instance_id", "") or 0)
                answer = form.get("answer", "")
                review.record(conn, instance_id, answer, reviewer=reviewer, note=form.get("note"))
                sample_id = conn.execute(
                    "SELECT sample_id FROM review_instances WHERE id = ?", (instance_id,)
                ).fetchone()[0]
                if sample_id is None:
                    return self._redirect(
                        "/review", notice=Notice(f"Spot check recorded: {answer}."), cookie=cookie
                    )
                nxt = review.pending(conn, sample_id)
                where = f"/review/instance/{nxt}" if nxt else f"/review/sample/{sample_id}"
                msg = f"Recorded {answer}." + ("" if nxt else " Sample complete.")
                return self._redirect(where, notice=Notice(msg), cookie=cookie)
            sample_id = int(form.get("sample_id", "") or 0)
            out = review.carry(conn, sample_id, reviewer=reviewer)
            msg = f"Confirmed {out['carried']} earlier verdict(s)."
            if out["conflicting"]:
                msg += f" {out['conflicting']} left pending: earlier answers disagree."
            return self._redirect(f"/review/sample/{sample_id}", notice=Notice(msg), cookie=cookie)
        except (review.ReviewError, ValueError) as exc:
            back = headers.get("referer") or "/review"
            back = urllib.parse.urlsplit(back)
            target = back.path if back.path.startswith("/review") else "/review"
            return self._redirect(target, notice=Notice(str(exc), is_error=True), cookie=cookie)
        finally:
            conn.close()
