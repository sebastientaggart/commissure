"""Fetch public documents into a solution's `intake/` (pre-ingest tools only).

For `recipe/tools/` fetchers, run by hand before ingest; no stage ever calls this, so the
pipeline stays offline. A solution's tool lists its sources; this module is how every source
is fetched, saved, and recorded:

- `Fetcher.get`: one request at a time per host, spaced by that host's delay (plus jitter),
  retried on transient failures, and stopped by `Blocked` at the first bot challenge or
  refusal (AWS WAF 202 with `x-amzn-waf-action`, 403, 429). It never poses as a browser: the
  User-Agent names the tool, plus `COMMISSURE_FETCH_CONTACT` when set.
- `save_document`: writes a PDF or HTML response only if it is one (`%PDF-`, an HTML
  declaration), and never over a file with different bytes (`changed_upstream`).
- `Manifest`: `intake/manifests/<source>.json`, outside every ingest folder: each document's
  URL, retrieval time, SHA-256, and status, saved after every document so a run resumes.
- `pdf_links`, `distinct_names`, `crawl_listing`, `fetch_pages`: a listing of pages, each
  page's own PDFs under the site's document `path_prefix` (off-site links recorded, never
  fetched), or the page itself as HTML when it has none (`html_only`).
- `import_saved`: when a site refuses scripts and a person saves its pages in a browser, match
  what was saved against the saved listings and report what is missing, unlisted, not a
  document, or saved twice. Nothing is moved, renamed, or deleted.
- `run`: the command line every fetcher shares.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED = 0, 1, 3
DEFAULT_DELAY = 2.0
MAX_ATTEMPTS = 3  # transient failures (5xx, timeouts, resets) only
MAX_LISTING_PAGES = 50  # a listing that never runs dry is a bug, not a corpus
USER_AGENT = "commissure-corpus-fetch/0.1 (public-document research)"


class Blocked(Exception):
    """The host answered with a bot challenge or a refusal: stop and resume later."""


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


Opener = Callable[[urllib.request.Request, float], Response]


def urllib_opener(req: urllib.request.Request, timeout: float) -> Response:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            headers = {k.lower(): v for k, v in r.headers.items()}
            return Response(r.status, r.geturl(), headers, r.read())
    except urllib.error.HTTPError as e:
        headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        return Response(e.code, e.geturl() or req.full_url, headers, e.read())


def is_challenge(resp: Response) -> bool:
    """AWS WAF's challenge is a 202 with `x-amzn-waf-action`; 403 and 429 are refusals."""
    return (
        "x-amzn-waf-action" in resp.headers
        or resp.status in (403, 429)
        or (resp.status == 202 and not resp.body)
    )


@dataclass
class Fetcher:
    """Polite GETs: `delays` seconds between requests to one host (`DEFAULT_DELAY` else)."""

    delays: Mapping[str, float] = field(default_factory=dict)
    opener: Opener = urllib_opener
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random
    timeout: float = 60.0
    last: dict[str, float] = field(default_factory=dict)
    requests: int = 0

    def get(self, url: str) -> Response:
        host = urllib.parse.urlsplit(url).hostname or ""
        delay = self.delays.get(host, DEFAULT_DELAY)
        contact = os.environ.get("COMMISSURE_FETCH_CONTACT", "").strip()
        agent = f"{USER_AGENT[:-1]}; {contact})" if contact else USER_AGENT
        resp: Response | None = None
        error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if host in self.last:
                # Up to a third more, so requests do not land on a fixed beat.
                wait = self.last[host] + delay * (1 + self.jitter() / 3) - self.clock()
                if wait > 0:
                    self.sleep(wait)
            req = urllib.request.Request(url, headers={"User-Agent": agent})
            try:
                resp, error = self.opener(req, self.timeout), None
            except (TimeoutError, OSError) as exc:
                resp, error = None, exc
            self.last[host] = self.clock()
            self.requests += 1
            if resp is not None and is_challenge(resp):
                raise Blocked(f"{host} challenged {url} (HTTP {resp.status})")
            if resp is not None and resp.status < 500:
                return resp
            if attempt < MAX_ATTEMPTS:
                self.sleep(delay * 2**attempt)
        if resp is None:
            assert error is not None
            raise error
        return resp


# --- manifest and files --------------------------------------------------------------------


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Manifest:
    path: Path
    data: dict

    @classmethod
    def load(cls, intake: Path, source: str) -> Manifest:
        path = intake / "manifests" / f"{source}.json"
        data = json.loads(path.read_text("utf-8")) if path.is_file() else {}
        data.setdefault("source", source)
        data.setdefault("documents", {})
        return cls(path, data)

    @property
    def docs(self) -> dict[str, dict]:
        return self.data["documents"]

    def save(self) -> None:
        self.data["updated_at"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", "utf-8")
        tmp.replace(self.path)


def safe_name(name: str, ext: str = ".pdf") -> str:
    """A file name from a URL basename or a key: no directories, no odd characters."""
    name = urllib.parse.unquote(Path(urllib.parse.urlsplit(name).path).name or name)
    name = re.sub(r"[^A-Za-z0-9._ ()&,+-]+", "_", name).strip(" ._") or "document"
    return name if name.lower().endswith(ext) else f"{name}{ext}"


HTML_MARK_RE = re.compile(rb"<(?:!doctype\s+html|html)\b", re.I)


def is_document(kind: str, body: bytes) -> bool:
    """A PDF starts `%PDF-`; an HTML page declares itself in its first 2 KB."""
    if kind == "pdf":
        return body.startswith(b"%PDF-")
    return HTML_MARK_RE.search(body[:2048]) is not None


def save_document(folder: Path, name: str, resp: Response, kind: str = "pdf") -> dict:
    """Write a PDF or HTML response under `folder`, never over different bytes. Returns its
    record; a response that is not the kind it should be (`not_pdf`, `not_html`) is not saved."""
    record = {"url": resp.url, "file": None, "retrieved_at": now(), "http_status": resp.status}
    if resp.status != 200:
        return record | {"status": f"http_{resp.status}"}
    if not is_document(kind, resp.body):
        return record | {"status": f"not_{kind}", "content_type": resp.headers.get("content-type")}
    sha = hashlib.sha256(resp.body).hexdigest()
    dest = folder / safe_name(name, f".{kind}")
    record |= {"file": dest.name, "sha256": sha, "bytes": len(resp.body)}
    if dest.exists():
        old = hashlib.sha256(dest.read_bytes()).hexdigest()
        return record | {"status": "unchanged" if old == sha else "changed_upstream"}
    folder.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(resp.body)
    tmp.replace(dest)
    return record | {"status": "saved"}


# What `save_document` reports when the file is on disk (`changed_upstream` keeps the earlier
# bytes, which a person resolves; fetching again would not change that).
KEPT = frozenset({"saved", "unchanged", "changed_upstream"})


def save_pdf(folder: Path, name: str, resp: Response) -> dict:
    return save_document(folder, name, resp, "pdf")


# --- pages ---------------------------------------------------------------------------------


def pdf_links(page: str, base: str, *, path_prefix: str) -> tuple[list[str], list[str]]:
    """The PDFs a page links to whose path starts with `path_prefix`, in order: `(own, offsite)`.

    `path_prefix` narrows links to the site's document store so stray PDFs in navigation or
    boilerplate are ignored. It has no default: a forgotten prefix would take every PDF on the
    host, navigation included, and nothing would say so. Only the page's own host is the
    source's document: a report links other agencies' PDFs as citations (and their sites may
    challenge scripts). Off-site links are returned so the manifest records them; they are not
    fetched.
    """
    own: list[str] = []
    offsite: list[str] = []
    host = urllib.parse.urlsplit(base).hostname
    for href in re.findall(r'href="([^"]+\.pdf)(?:[?#][^"]*)?"', page, re.I):
        url = urllib.parse.urljoin(base, html.unescape(href))
        split = urllib.parse.urlsplit(url)
        if not split.path.startswith(path_prefix) or url in own or url in offsite:
            continue
        (own if split.hostname == host else offsite).append(url)
    return own, offsite


def distinct_names(names: Sequence[str], urls: Sequence[str]) -> list[str]:
    """`names` with later repeats made unique by a hash of their URL. Two different PDFs can
    share a file name in two folders; the first keeps its name, so a rerun names every file
    as before."""
    out: list[str] = []
    for name, url in zip(names, urls, strict=True):
        if name in out:
            stem = name.removesuffix(".pdf")
            name = f"{stem}--{hashlib.sha256(url.encode()).hexdigest()[:8]}.pdf"
        out.append(name)
    return out


def page_text(page: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


def page_title(page: str) -> str | None:
    """The page's `<h1>`, else its `<title>` without a trailing ` | Site Name`."""
    if m := re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I):
        return page_text(m.group(1)).strip() or None
    if m := re.search(r"<title>(.*?)</title>", page, re.S | re.I):
        return re.sub(r"\s+\|\s+[^|]+$", "", page_text(m.group(1)).strip()) or None
    return None


CANONICAL_RE = re.compile(
    r'<(?:link[^>]+rel="canonical"[^>]+href|meta[^>]+property="og:url"[^>]+content)='
    r'"(https?://[^"]+)"',
    re.I,
)


def canonical_url(page: str) -> str | None:
    """The address a saved page says it is (`<link rel="canonical">`, else `og:url`)."""
    m = CANONICAL_RE.search(page)
    return html.unescape(m.group(1)) if m else None


def crawl_listing(
    f: Fetcher, base: str, path: str, link_re: str, *, max_pages: int = MAX_LISTING_PAGES
) -> list[str]:
    """Item paths from a paginated listing (`?page=0`, `?page=1`, ...) until a page adds none."""
    found: list[str] = []
    for page in range(max_pages):
        resp = f.get(f"{base}{path}?page={page}")
        new = [p for p in dict.fromkeys(re.findall(link_re, resp.text)) if p not in found]
        if resp.status != 200 or not new:
            return found
        found += new
    raise RuntimeError(f"{base}{path} still had new items after {max_pages} pages")


def fetch_pages(
    f: Fetcher,
    m: Manifest,
    base: str,
    paths: Sequence[str],
    folder: Path,
    limit: int | None,
    name_for: Callable[[str, str, int, int], str],
    meta_for: Callable[[str, str], dict],
    *,
    path_prefix: str,
) -> None:
    """Each page's own PDFs under `path_prefix` into `folder`, or the page itself as
    `<slug>.html` when it has none.

    A page already `done` or `html_only` is skipped, so an interrupted run resumes; a page with
    a file that was not saved is `incomplete` and fetched again on the next run; `name_for`
    names the PDFs (page path, PDF URL, index, count) and `meta_for` reads what the manifest
    should say about a page.
    """
    done = 0
    for path in paths:
        if m.docs.get(path, {}).get("status") in ("done", "html_only"):
            continue
        if limit is not None and done >= limit:
            break
        resp = f.get(base + path)
        entry = {"page_url": base + path, "retrieved_at": now(), "http_status": resp.status}
        if resp.status != 200:
            m.docs[path] = entry | {"status": f"http_{resp.status}"}
        else:
            links, offsite = pdf_links(resp.text, base + path, path_prefix=path_prefix)
            entry |= meta_for(path, resp.text)
            names = distinct_names(
                [safe_name(name_for(path, url, i, len(links))) for i, url in enumerate(links)],
                links,
            )
            files = [save_pdf(folder, n, f.get(url)) for n, url in zip(names, links, strict=True)]
            entry["pdfs"] = files
            if not files:
                # No PDF of its own: the page is the document (core parses HTML).
                slug = path.rstrip("/").rsplit("/", 1)[-1]
                entry["page_file"] = save_document(folder, slug, resp, "html")
            # Finished only when every document is on disk: a page with a failed file is
            # `incomplete`, so the next run fetches it again instead of skipping it forever.
            kept = all(r["status"] in KEPT for r in files or [entry["page_file"]])
            entry["status"] = ("done" if files else "html_only") if kept else "incomplete"
            m.docs[path] = entry | ({"offsite_pdfs": offsite} if offsite else {})
        m.save()
        done += 1


# --- documents a person saved --------------------------------------------------------------


def import_saved(
    m: Manifest,
    folder: Path,
    listed: Mapping[str, dict],
    *,
    slug_of: Callable[[str], str],
    identify_page: Callable[[str], str | None],
    retrieved_by: str,
) -> dict[str, list[str]]:
    """Match the files a person saved in `folder` against the `listed` documents (key -> what
    the saved listings say), record them in `m`, and return the problems by kind.

    A PDF matches a document whose `slug_of(key)` is its file name, or starts it followed by
    `-` (an attachment: `<slug>-attachment-a.pdf`). A saved HTML page matches by
    `identify_page(text)` (its canonical URL, a number printed in it), whatever the browser
    named the file. Problems: `missing` (listed, nothing saved), `unlisted` (saved, no listing
    names it), `not_a_document` (neither PDF nor HTML), `pdf_and_html` (a document saved both
    as its page and as its own PDF: one document twice). Nothing is moved or deleted.
    """
    files = sorted(p for p in folder.glob("*") if p.is_file() and not p.name.startswith("."))
    problems: dict[str, list[str]] = {
        "missing": [], "unlisted": [], "not_a_document": [], "pdf_and_html": [],
    }  # fmt: skip
    by_slug: dict[str, list[dict]] = {}
    pages: dict[str, list[dict]] = {}
    unmatched: list[dict] = []
    for p in files:
        body = p.read_bytes()
        kind = {".pdf": "pdf", ".html": "html", ".htm": "html"}.get(p.suffix.lower())
        if kind is None or not is_document(kind, body):
            problems["not_a_document"].append(p.name)
            continue
        saved = datetime.fromtimestamp(p.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {"file": p.name, "kind": kind, "sha256": hashlib.sha256(body).hexdigest(),
                  "bytes": len(body), "saved_at": saved}  # fmt: skip
        if kind == "pdf":
            by_slug.setdefault(p.stem.lower(), []).append(record)
        elif key := identify_page(body.decode("utf-8", errors="replace")):
            pages.setdefault(key, []).append(record | {"identified_by": "page"})
        else:
            unmatched.append(record)
    docs: dict[str, dict] = {}
    for key, entry in sorted(listed.items()):
        slug = slug_of(key).lower()
        own_pdf = by_slug.pop(slug, [])
        pdfs = list(own_pdf)
        for other in [k for k in by_slug if k.startswith(slug + "-")]:
            pdfs += by_slug.pop(other)
        saved_pages = pages.pop(key, [])
        status = "saved_manually" if pdfs or saved_pages else "missing"
        docs[key] = dict(entry) | {"pdfs": pdfs, "pages": saved_pages, "status": status}
        if not (pdfs or saved_pages):
            problems["missing"].append(key)
        elif own_pdf and saved_pages:
            # The page and its own PDF are one document twice; a page beside its attachments'
            # PDFs is the normal case.
            problems["pdf_and_html"].append(key)
    leftovers = [r for rs in by_slug.values() for r in rs] + unmatched
    leftovers += [r for rs in pages.values() for r in rs]  # a page no listing names
    for r in sorted(leftovers, key=lambda r: r["file"]):
        docs[f"file:{r['file']}"] = {"files": [r], "status": "unlisted"}
        problems["unlisted"].append(r["file"])
    m.data |= {"retrieved_by": retrieved_by, "imported_at": now()}
    m.data["documents"] = docs
    m.data["not_a_document"] = problems["not_a_document"]
    m.save()
    return problems


# --- command line --------------------------------------------------------------------------


def summary(intake: Path, source: str) -> str:
    docs = Manifest.load(intake, source).docs
    counts: dict[str, int] = {}
    for d in docs.values():
        counts[d.get("status", "?")] = counts.get(d.get("status", "?"), 0) + 1
    return f"{source}: {len(docs)} entries {dict(sorted(counts.items()))}"


def run(
    argv: Sequence[str] | None,
    *,
    description: str,
    intake: Path,
    sources: Mapping[str, Callable[[Fetcher, Path, int | None], None]],
    fetcher: Fetcher,
    opt_in: frozenset[str] = frozenset(),
    imports: Mapping[str, tuple[str, Callable[[Path], dict[str, list[str]]]]] | None = None,
) -> int:
    """The fetcher command line: `<tool> all | <source>... [--intake DIR] [--limit N]`.

    `all` runs every source but the `opt_in` ones (a site known to refuse scripts). `imports`
    maps a command to `(manifest source, function)` for documents a person saved by hand.
    Exit 0 when every source finished, 1 when any source failed, and 3 only when a
    source was stopped by a challenge and none failed (resume later).
    """
    imports = dict(imports or {})
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("sources", nargs="+", choices=[*sources, *imports, "all"])
    ap.add_argument("--intake", type=Path, default=intake)
    ap.add_argument("--limit", type=int, default=None, help="documents per source (a slice)")
    args = ap.parse_args(argv)
    names = [n for n in sources if n not in opt_in] if "all" in args.sources else args.sources
    code = EXIT_OK
    for name in names:
        try:
            if name in imports:
                source, fn = imports[name]
                for kind, which in fn(args.intake).items():
                    if which:
                        print(f"{name}: {len(which)} {kind}: {', '.join(which)}")
                print(summary(args.intake, source))
                continue
            sources[name](fetcher, args.intake, args.limit)
        except Blocked as exc:
            print(f"{name}: stopped, {exc}; progress is saved, run again later to resume",
                  file=sys.stderr)  # fmt: skip
            # A failure needs a person; a block clears on the next run, so 1 outranks 3.
            code = code if code == EXIT_ERROR else EXIT_BLOCKED
        except Exception as exc:  # report and go on to the next source
            print(f"{name}: failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            code = EXIT_ERROR
        print(summary(args.intake, name))
    print(f"{fetcher.requests} requests")
    return code
