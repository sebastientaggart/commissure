"""`commissure.patterns.fetch` against a fake web: no network, no sleeping.

Each rule runs on its input and on a sibling it must leave alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from commissure.patterns.fetch import (
    EXIT_BLOCKED,
    EXIT_OK,
    USER_AGENT,
    Fetcher,
    Manifest,
    Response,
    canonical_url,
    crawl_listing,
    distinct_names,
    fetch_pages,
    import_saved,
    is_challenge,
    is_document,
    page_title,
    pdf_links,
    run,
    save_document,
    save_pdf,
)

PDF = b"%PDF-1.7\n%fake\n"
CHALLENGE = (202, {"x-amzn-waf-action": "challenge"}, b"")
SITE = "https://agency.example.gov"


class FakeWeb:
    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, req, timeout):
        url = req.full_url
        self.calls.append(url)
        page = self.pages.get(url, (404, {}, b"not found"))
        if isinstance(page, str):
            page = (200, {"content-type": "text/html"}, page.encode())
        elif isinstance(page, bytes):
            page = (200, {"content-type": "application/pdf"}, page)
        status, headers, body = page
        return Response(status, url, headers, body)


def fetcher(web) -> Fetcher:
    return Fetcher(opener=web, sleep=lambda s: None, clock=lambda: 0.0, jitter=lambda: 0.0)


def html_page(body: str, head: str = "") -> str:
    return f"<!DOCTYPE html><html><head>{head}</head><body><main>{body}</main></body></html>"


def test_challenges_and_refusals_stop_and_ordinary_answers_do_not():
    for status, headers in ((202, {"x-amzn-waf-action": "challenge"}), (429, {}), (403, {})):
        assert is_challenge(Response(status, "u", headers, b"<html>"))
    assert not is_challenge(Response(200, "u", {}, b"<html>"))
    assert not is_challenge(Response(404, "u", {}, b"gone"))


def test_save_writes_only_its_kind_and_never_overwrites_different_bytes(tmp_path):
    ok = save_pdf(tmp_path, "a.pdf", Response(200, "https://x/a.pdf", {}, PDF))
    assert ok["status"] == "saved" and (tmp_path / "a.pdf").read_bytes() == PDF
    assert save_pdf(tmp_path, "a.pdf", Response(200, "u", {}, PDF))["status"] == "unchanged"
    changed = save_pdf(tmp_path, "a.pdf", Response(200, "u", {}, PDF + b"x"))
    assert changed["status"] == "changed_upstream"
    assert (tmp_path / "a.pdf").read_bytes() == PDF  # the original stays as it was
    html = save_pdf(tmp_path, "b.pdf", Response(200, "u", {}, b"<html>"))
    assert html["status"] == "not_pdf" and not (tmp_path / "b.pdf").exists()
    gone = save_pdf(tmp_path, "c.pdf", Response(404, "u", {}, b""))
    assert gone["status"] == "http_404" and gone["file"] is None
    page = save_document(tmp_path, "report", Response(200, "u", {}, b"<!doctype html><p>x"), "html")
    assert (page["status"], page["file"]) == ("saved", "report.html")
    stray = save_document(tmp_path, "n", Response(200, "u", {}, b"plain text"), "html")
    assert stray["status"] == "not_html"
    assert is_document("pdf", PDF) and not is_document("pdf", b"<html>")


def test_the_user_agent_names_the_tool_and_a_contact_only_when_asked(monkeypatch):
    seen = []

    def opener(req, timeout):
        seen.append(req.get_header("User-agent"))
        return Response(200, req.full_url, {}, b"ok")

    f = fetcher(opener)
    monkeypatch.delenv("COMMISSURE_FETCH_CONTACT", raising=False)
    f.get("https://example.gov/a")
    monkeypatch.setenv("COMMISSURE_FETCH_CONTACT", "ops@example.org")
    f.get("https://example.gov/b")
    assert seen == [
        USER_AGENT,
        "commissure-corpus-fetch/0.1 (public-document research; ops@example.org)",
    ]


def test_requests_to_one_host_are_spaced_by_its_delay_and_challenges_raise():
    waits: list[float] = []
    t = {"now": 0.0}
    f = Fetcher(
        delays={"slow.example.gov": 45.0},
        opener=lambda req, timeout: Response(200, req.full_url, {}, b"ok"),
        sleep=lambda s: (waits.append(s), t.__setitem__("now", t["now"] + s)),
        clock=lambda: t["now"],
        jitter=lambda: 0.0,
    )
    f.get("https://slow.example.gov/a")
    f.get("https://slow.example.gov/b")
    f.get("https://other.example.gov/c")  # another host: no wait
    assert waits == [pytest.approx(45.0)]
    blocked = fetcher(FakeWeb({"https://w.example.gov/": CHALLENGE}))
    with pytest.raises(Exception, match="challenged"):
        blocked.get("https://w.example.gov/")


def test_page_links_are_the_pages_own_and_repeated_names_stay_apart():
    page = (
        '<a href="/sites/default/files/a/brief.pdf">a</a>'
        '<a href="/sites/default/files/b/brief.pdf">b</a>'
        '<a href="https://other.example.gov/sites/default/files/c.pdf">cited</a>'
        '<a href="/files/not-a-document-store.pdf">x</a>'
    )
    own, offsite = pdf_links(page, f"{SITE}/reports/r1")
    assert own == [
        f"{SITE}/sites/default/files/a/brief.pdf",
        f"{SITE}/sites/default/files/b/brief.pdf",
    ]
    assert offsite == ["https://other.example.gov/sites/default/files/c.pdf"]
    names = distinct_names(["brief.pdf", "brief.pdf"], own)
    assert names[0] == "brief.pdf" and names[1].startswith("brief--") and names[1] != names[0]


def test_titles_and_canonical_urls_come_from_the_page():
    assert page_title("<title>Rule Notice | Example Agency</title>") == "Rule Notice"
    assert page_title("<h1>Heading <em>One</em></h1><title>T | S</title>") == "Heading One"
    head = '<link rel="canonical" href="https://agency.example.gov/policy/a-1" />'
    assert canonical_url(html_page("x", head)) == "https://agency.example.gov/policy/a-1"
    og = '<meta property="og:url" content="https://agency.example.gov/policy/b-2" />'
    assert canonical_url(html_page("x", og)) == "https://agency.example.gov/policy/b-2"
    assert canonical_url(html_page("x")) is None


LISTED_SITE = {
    f"{SITE}/notices?page=0": '<a href="/notices/n-1">1</a><a href="/notices/n-2">2</a>',
    f"{SITE}/notices?page=1": '<a href="/notices/n-2">2</a><a href="/notices/n-3">3</a>',
    f"{SITE}/notices?page=2": '<a href="/notices/n-3">3</a>',
    f"{SITE}/notices/n-1": html_page('<a href="/sites/default/files/n-1.pdf">PDF</a>'),
    f"{SITE}/sites/default/files/n-1.pdf": PDF,
    f"{SITE}/notices/n-2": html_page("<p>Only on the page.</p>"),
    f"{SITE}/notices/n-3": html_page('<a href="/sites/default/files/n-3.pdf">PDF</a>'),
    f"{SITE}/sites/default/files/n-3.pdf": PDF + b"3",
}


def _pages(f: Fetcher, intake: Path, limit: int | None = None) -> Manifest:
    m = Manifest.load(intake, "notices")
    paths = crawl_listing(f, SITE, "/notices", r'href="(/notices/n-\d)"')

    def meta(path: str, page: str) -> dict:
        return {"title": page_title(page)}

    fetch_pages(f, m, SITE, paths, intake / "notices", limit, lambda p, url, i, n: url, meta)
    return Manifest.load(intake, "notices")


def test_listings_crawl_until_dry_and_pages_without_a_pdf_are_kept_as_html(tmp_path):
    m = _pages(fetcher(FakeWeb(LISTED_SITE)), tmp_path)
    assert sorted(p.name for p in (tmp_path / "notices").iterdir()) == [
        "n-1.pdf",
        "n-2.html",
        "n-3.pdf",
    ]
    assert m.docs["/notices/n-2"]["status"] == "html_only"
    assert m.docs["/notices/n-2"]["page_file"]["status"] == "saved"
    assert m.docs["/notices/n-1"]["status"] == "done"


def test_a_challenge_stops_the_run_and_the_next_run_resumes_without_refetching(tmp_path):
    blocked = dict(LISTED_SITE) | {f"{SITE}/notices/n-2": CHALLENGE}
    first = FakeWeb(blocked)
    with pytest.raises(Exception, match="challenged"):
        _pages(fetcher(first), tmp_path)
    assert f"{SITE}/notices/n-3" not in first.calls  # stopped at the challenge
    assert Manifest.load(tmp_path, "notices").docs["/notices/n-1"]["status"] == "done"
    second = FakeWeb(LISTED_SITE)
    _pages(fetcher(second), tmp_path)
    assert f"{SITE}/notices/n-1" not in second.calls  # done pages are not fetched again
    assert (tmp_path / "notices" / "n-3.pdf").is_file()


def test_a_page_with_a_file_not_saved_is_incomplete_and_fetched_again(tmp_path):
    failing = dict(LISTED_SITE) | {
        f"{SITE}/sites/default/files/n-1.pdf": (500, {}, b"server error"),
        f"{SITE}/notices/n-2": (200, {"content-type": "application/json"}, b"{}"),
    }
    m = _pages(fetcher(FakeWeb(failing)), tmp_path)
    assert m.docs["/notices/n-1"]["status"] == "incomplete"  # its PDF failed
    assert m.docs["/notices/n-2"]["status"] == "incomplete"  # the page was not HTML
    assert m.docs["/notices/n-3"]["status"] == "done"
    second = FakeWeb(LISTED_SITE)
    m = _pages(fetcher(second), tmp_path)
    assert f"{SITE}/notices/n-3" not in second.calls  # finished pages stay skipped
    assert (m.docs["/notices/n-1"]["status"], m.docs["/notices/n-2"]["status"]) == (
        "done",
        "html_only",
    )
    assert (tmp_path / "notices" / "n-1.pdf").is_file()


def test_saved_files_match_by_name_or_by_page_and_problems_are_reported(tmp_path):
    folder = tmp_path / "saved"
    folder.mkdir()
    listed = {f"/notices/n-{i}": {"title": f"Notice {i}"} for i in range(1, 5)}
    (folder / "n-1.pdf").write_bytes(PDF)
    (folder / "n-1-attachment-a.pdf").write_bytes(PDF + b"a")
    (folder / "Notice 2 _ Example Agency.html").write_text(
        html_page("x", '<link rel="canonical" href="https://agency.example.gov/notices/n-2">'),
        "utf-8",
    )
    (folder / "n-3.pdf").write_bytes(PDF + b"3")
    (folder / "Notice 3.html").write_text(
        html_page("x", '<link rel="canonical" href="https://agency.example.gov/notices/n-3">'),
        "utf-8",
    )
    (folder / "z-9.pdf").write_bytes(PDF + b"z")
    (folder / "notes.txt").write_text("not a document")

    def identify(text: str) -> str | None:
        url = canonical_url(text)
        return url.removeprefix("https://agency.example.gov") if url else None

    m = Manifest.load(tmp_path, "notices")
    problems = import_saved(m, folder, listed, slug_of=lambda k: k.rsplit("/", 1)[-1],
                            identify_page=identify, retrieved_by="hand")  # fmt: skip
    assert problems == {
        "missing": ["/notices/n-4"],
        "unlisted": ["z-9.pdf"],
        "not_a_document": ["notes.txt"],
        "pdf_and_html": ["/notices/n-3"],  # its page and its own PDF: one document twice
    }
    docs = json.loads(m.path.read_text())["documents"]
    assert [p["file"] for p in docs["/notices/n-1"]["pdfs"]] == ["n-1.pdf", "n-1-attachment-a.pdf"]
    assert docs["/notices/n-2"]["pages"][0]["file"] == "Notice 2 _ Example Agency.html"
    assert docs["/notices/n-1"]["status"] == "saved_manually" and (folder / "notes.txt").exists()


def test_run_skips_opt_in_sources_for_all_and_exits_3_when_blocked(tmp_path, capsys):
    ran: list[str] = []

    def ok(name):
        return lambda f, intake, limit: ran.append(name)

    def blocked(f, intake, limit):
        fetcher(FakeWeb({"https://w.example.gov/": CHALLENGE})).get("https://w.example.gov/")

    sources = {"a": ok("a"), "b": ok("b"), "refuses": ok("refuses")}
    common = {"description": "t", "intake": tmp_path, "fetcher": fetcher(FakeWeb({}))}
    assert run(["all"], sources=sources, opt_in=frozenset({"refuses"}), **common) == EXIT_OK
    assert ran == ["a", "b"]
    assert run(["x"], sources={"x": blocked}, **common) == EXIT_BLOCKED
    assert "progress is saved" in capsys.readouterr().err
