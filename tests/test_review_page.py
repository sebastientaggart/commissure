"""The review page in `commissure serve`: samples, verdicts, carry-over, and sandboxed originals.

A small recipe with an `INTERFACE` and one check (`cite:/quotes/*`) runs over JSON items
(one quoting CSS, to prove corpus text cannot trip the offline guard), a generated native
PDF, and HTML and SVG files that are never parsed but must still be served safely.
"""

import json
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from commissure.api import page_external_resource_count
from commissure.review_page import render_json_tree
from commissure.serve import InterfaceApp, make_server
from commissure.workspace import open_workspace

PROBE = Path(__file__).parent / "fixtures" / "probe_corpus"
CSS_QUOTE = (
    "Servers should never load url(https://cdn.example.com/a.css) or @import https://x.example"
)
HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from commissure.workspace import lock

    with lock(Path(sys.argv[1])):
        print("locked", flush=True)
        sys.stdin.readline()
    """
)
RECIPE = """
from pydantic import BaseModel, ConfigDict, Field
from commissure.api import Check, Evidence, Interface, RecordEnvelope, Diagnostic, Stage


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str
    evidence: list[Evidence] = Field(min_length=1)


class Cited(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quotes: list[Quote]


def cite(ctx, item):
    words = (item.text or "").split()
    if len(words) < 6:
        return RecordEnvelope.skipped("too_short", Diagnostic(code="too_short"))
    needle = " ".join(words[3:9])  # past the JSON key and the opening quote
    start = item.text.find(needle)
    if start < 0:
        return RecordEnvelope.skipped("not_contiguous", Diagnostic(code="not_contiguous"))
    span = Evidence.span(item, start, start + len(needle), kind="quote")
    return Cited(quotes=[Quote(quote=needle, evidence=[span])])


STAGES = [
    Stage(
        "cite",
        cite,
        schema=Cited,
        text="default",
        checks=(Check("/quotes/*", "Does the item say this?"),),
    )
]

INTERFACE = Interface(title="Review Console", render=lambda view: "<p>home</p>")
"""


def write_corpus(src: Path) -> None:
    src.mkdir()
    (src / "css.json").write_text(json.dumps({"comment": CSS_QUOTE}), encoding="utf-8")
    for name in ("one", "two", "three"):
        body = f"The {name} commenter writes that servicers need more time to comply."
        (src / f"{name}.json").write_text(json.dumps({"comment": body}), encoding="utf-8")
    shutil.copy(PROBE / "native.pdf", src / "letter.pdf")
    (src / "broken.json").write_text('{"comment": "not closed', encoding="utf-8")
    (src / "evil.html").write_text(
        "<html><script>fetch('/').then(r => r.text())</script></html>", encoding="utf-8"
    )
    (src / "evil.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        encoding="utf-8",
    )


@pytest.fixture
def app(cli, tmp_path):
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "recipe.py").write_text(RECIPE, encoding="utf-8")
    ws_root = tmp_path / "workspace"
    write_corpus(tmp_path / "src")
    assert cli.ingest(ws_root, tmp_path / "src")[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root)).exit_code == 0
    res = cli("run", "--step", "cite", "--workspace", str(ws_root), "--recipe", str(recipe))
    assert res.exit_code == 0, res.output
    ui = InterfaceApp(ws_root, recipe)
    yield ui
    ui.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@contextmanager
def running(app):
    server = make_server(app, host="127.0.0.1", port=0)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def http(url, *, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=10) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        with err:
            return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def page(base, path):
    status, headers, body = http(base + path)
    assert status == 200, (path, status, body[:300])
    html = body.decode("utf-8")
    assert page_external_resource_count(html) == 0, path
    return html


def counts(app, sql):
    ws = open_workspace(app.workspace_root)
    try:
        return ws.conn.execute(sql).fetchone()[0]
    finally:
        ws.close()


def draw(base, app, n="10", reviewer="Pat Reviewer"):
    form = {"csrf_token": app.csrf_token, "check": "cite:/quotes/*", "n": n, "reviewer": reviewer}
    return http(base + "/review/sample", form=form)


def record(base, app, instance_id, answer, reviewer="Pat Reviewer", note=""):
    form = {
        "csrf_token": app.csrf_token,
        "instance_id": str(instance_id),
        "answer": answer,
        "reviewer": reviewer,
        "note": note,
    }
    return http(base + "/review/record", form=form)


def test_a_sample_is_drawn_judged_and_rated_through_the_page(app):
    with running(app) as base:
        home = page(base, "/")
        assert '<a class="header-icon-btn" href="/review" aria-label="Review results"' in home
        index = page(base, "/review")
        assert "cite:/quotes/*" in index and "No samples drawn yet" in index

        status, headers, _ = draw(base, app)
        assert status == 303
        assert headers["location"].startswith("/review/instance/")
        assert "commissure_reviewer=Pat%20Reviewer" in headers["set-cookie"]
        location = headers["location"]
        seen = 0
        while location.startswith("/review/instance/"):
            html = page(base, location)
            assert "Does the item say this?" in html and "<mark>" in html
            instance_id = int(location.split("/")[3].split("?")[0])
            status, headers, _ = record(base, app, instance_id, "wrong" if seen == 0 else "correct")
            assert status == 303
            location = headers["location"]
            seen += 1
        assert seen == 5  # four JSON items and the PDF
        assert location.startswith("/review/sample/")

        done = page(base, location.split("?")[0])
        assert "1</strong> wrong" in done and "0</strong> pending" in done
        assert "20.0%" in done  # 1 wrong of 5 judged
    assert counts(app, "SELECT count(*) FROM verdicts") == 5


def test_the_instance_page_shows_the_original_beside_the_claim(app):
    with running(app) as base:
        draw(base, app)
        ws = open_workspace(app.workspace_root)
        try:
            instance_id, version_id = ws.conn.execute(
                "SELECT i.id, e.item_version_id FROM review_instances i"
                " JOIN outcomes o ON o.id = i.outcome_id"
                " JOIN evidence e ON e.record_id = o.record_id"
                " JOIN item_versions v ON v.id = e.item_version_id"
                " JOIN items it ON it.id = v.item_id WHERE it.locator = 'letter.pdf'"
            ).fetchone()
        finally:
            ws.close()
        html = page(base, f"/review/instance/{instance_id}")
        [src] = re.findall(r'<iframe[^>]*src="([^"]+)"', html)
        assert src == f"/review/original/{version_id}#page=1"
        assert "letter.pdf, p. 1" in html


def test_corpus_text_cannot_trip_the_offline_guard(app):
    """The quote and its context contain `url(https:` and `@import https:`; the page still
    renders, reads the same, and the guard finds no external resource in it."""
    with running(app) as base:
        draw(base, app)
        ws = open_workspace(app.workspace_root)
        try:
            [instance_id] = ws.conn.execute(
                "SELECT i.id FROM review_instances i JOIN items it"
                " ON 'item:' || it.id = i.subject_key WHERE it.locator = 'css.json'"
            ).fetchone()
        finally:
            ws.close()
        html = page(base, f"/review/instance/{instance_id}")
        assert "url&#40;https://cdn.example.com/a.css" in html
        assert "&#64;import" in html and "https://x.example" in html
        assert "url(https://cdn.example.com" not in html


def test_originals_are_sandboxed_and_only_inert_types_display(app):
    ws = open_workspace(app.workspace_root)
    try:
        versions = dict(ws.conn.execute("SELECT locator, item_version_id FROM current_items"))
    finally:
        ws.close()
    with running(app) as base:
        status, headers, body = http(base + f"/review/original/{versions['letter.pdf']}")
        assert status == 200
        assert headers["content-security-policy"] == "sandbox"
        assert headers["content-type"] == "application/pdf"
        assert headers["content-disposition"].startswith("inline;")
        assert body == (PROBE / "native.pdf").read_bytes()  # streamed, byte for byte
        assert int(headers["content-length"]) == len(body)
        for name in ("evil.html", "evil.svg"):
            status, headers, body = http(base + f"/review/original/{versions[name]}")
            assert status == 200
            assert headers["content-security-policy"] == "sandbox"
            assert headers["content-type"] == "application/octet-stream"
            assert headers["content-disposition"].startswith(f'attachment; filename="{name}"')
            assert headers["x-content-type-options"] == "nosniff"
        status, headers, _ = http(base + "/review/original/99999")
        assert status == 404


def test_review_posts_need_the_csrf_token(app):
    with running(app) as base:
        draw(base, app)
        for token in (None, "wrong"):
            form = {"instance_id": "1", "answer": "correct", "reviewer": "Pat"}
            if token:
                form["csrf_token"] = token
            status, _, _ = http(base + "/review/record", form=form)
            assert status == 403
    assert counts(app, "SELECT count(*) FROM verdicts") == 0


def test_a_missing_reviewer_is_refused_with_a_notice_and_nothing_recorded(app):
    with running(app) as base:
        status, headers, _ = draw(base, app, reviewer="")
        assert status == 303 and headers["location"].startswith("/review")
        assert "a reviewer is required" in page(base, headers["location"])
    assert counts(app, "SELECT count(*) FROM review_samples") == 0


def test_verdicts_record_while_a_run_holds_the_workspace_and_the_page_does_not_poll(app):
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(app.workspace_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with running(app) as base:
            assert app._run_mutex.acquire(blocking=False)  # an interface run, too
            try:
                status, headers, _ = draw(base, app)
                assert status == 303
                html = page(base, headers["location"].split("?")[0])
                assert "Run in progress" in html
                assert 'data-busy="false"' in html  # report.js must not disable the form
                assert 'hx-get="/_status"' not in html and "pipeline-drawer" not in html
                instance_id = int(headers["location"].split("/")[3].split("?")[0])
                status, _, _ = record(base, app, instance_id, "correct")
                assert status == 303
                home = page(base, "/")
                assert 'data-busy="true"' in home and 'hx-get="/_status"' in home
            finally:
                app._run_mutex.release()
    finally:
        holder.communicate("\n", timeout=10)
    assert counts(app, "SELECT count(*) FROM verdicts") == 1


def test_reviewer_cookie_overrides_env_and_unmigrated_db_shows_empty_state(
    app, tmp_path, monkeypatch
):
    monkeypatch.setenv("COMMISSURE_REVIEWER", "Env Default")
    with running(app) as base:
        assert 'value="Env Default"' in page(base, "/review")
        status, headers, _ = draw(base, app, reviewer="Cookie Override")
        assert status == 303
        cookie = headers["set-cookie"].split(";", 1)[0]
        req = urllib.request.Request(base + "/review", headers={"Cookie": cookie})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")
        assert 'value="Cookie Override"' in html

    unmigrated = tmp_path / "unmigrated"
    ws = open_workspace(unmigrated)
    ws.conn.execute("PRAGMA user_version = 1")
    ws.close()
    unmigrated_app = InterfaceApp(unmigrated, app.recipe_path)
    try:
        with running(unmigrated_app) as base:
            assert "Nothing to review yet" in page(base, "/review")
    finally:
        unmigrated_app.shutdown()


def test_earlier_verdicts_count_only_once_confirmed_in_bulk(app):
    with running(app) as base:
        _, first, _ = draw(base, app)
        location = first["location"]
        while location.startswith("/review/instance/"):
            instance_id = int(location.split("/")[3].split("?")[0])
            location = record(base, app, instance_id, "wrong", note="nope")[1]["location"]
        _, second, _ = draw(base, app)
        ws = open_workspace(app.workspace_root)
        try:
            sample_id = ws.conn.execute("SELECT max(id) FROM review_samples").fetchone()[0]
        finally:
            ws.close()
        before = page(base, f"/review/sample/{sample_id}")
        assert "Confirm 5 earlier verdict(s)" in before and "0</strong> wrong" in before
        form = {"csrf_token": app.csrf_token, "sample_id": str(sample_id), "reviewer": "Sam"}
        status, headers, _ = http(base + "/review/carry", form=form)
        assert status == 303
        after = page(base, headers["location"].split("?")[0])
        assert "5</strong> wrong" in after and "0</strong> pending" in after

        bad_carry = {"csrf_token": app.csrf_token, "sample_id": "999", "reviewer": "Sam"}
        status, headers, _ = http(base + "/review/carry", form=bad_carry)
        assert status == 303 and headers["location"].startswith("/review?notice=")
        assert "no sample 999" in page(base, headers["location"])
    notes = counts(
        app,
        "SELECT count(*) FROM verdicts WHERE reviewer = 'Sam'"
        " AND note LIKE 'confirmed earlier verdict(s) %'",
    )
    assert notes == 5


def test_json_originals_open_as_a_collapsible_tree_with_the_quote_marked(app):
    ws = open_workspace(app.workspace_root)
    try:
        versions = dict(ws.conn.execute("SELECT locator, item_version_id FROM current_items"))
    finally:
        ws.close()
    with running(app) as base:
        draw(base, app)
        ws = open_workspace(app.workspace_root)
        try:
            [instance_id] = ws.conn.execute(
                "SELECT i.id FROM review_instances i JOIN items it"
                " ON 'item:' || it.id = i.subject_key WHERE it.locator = 'one.json'"
            ).fetchone()
        finally:
            ws.close()
        html = page(base, f"/review/instance/{instance_id}")
        [src] = re.findall(r'<iframe[^>]*src="([^"]+)"', html)
        quote = "one commenter writes that servicers need"
        assert src == f"/review/tree/{versions['one.json']}?mark={urllib.parse.quote(quote)}"
        assert f'href="/review/original/{versions["one.json"]}"' in html  # raw text kept

        status, headers, body = http(base + src)
        assert status == 200
        assert headers["content-security-policy"] == "sandbox"
        assert headers["content-type"] == "text/html; charset=utf-8"
        tree = body.decode()
        assert tree.count("<mark>") == 1 and f"<mark>{quote}</mark>" in tree
        assert "<details open><summary>" in tree and "<script" not in tree

        css = page(base, f"/review/tree/{versions['css.json']}")
        assert "url&#40;https://cdn.example.com/a.css" in css

        broken = page(base, f"/review/tree/{versions['broken.json']}")
        assert (
            "Not valid JSON" in broken and f"/review/original/{versions['broken.json']}" in broken
        )
        assert http(base + f"/review/tree/{versions['letter.pdf']}")[0] == 404


def test_the_tree_escapes_values_and_opens_only_the_path_to_a_mark():
    doc = {
        "deep": {"er": {"still": {"here": "find the needle in <b>this</b>", "other": "x"}}},
        "side": {"a": {"b": {"c": "no mark down here"}}},
        "script": "<script>alert(1)</script>",
        "n": 3,
        "flag": None,
    }
    html = render_json_tree(doc, "needle")
    assert "&lt;script&gt;alert&#40;1)&lt;/script&gt;" in html and "<script" not in html
    assert html.count("<mark>needle</mark>") == 1
    assert "&lt;b&gt;this&lt;/b&gt;" in html and "<b>" not in html  # escaped beside a mark
    # Depth 0 and 1 open by default; the path to the mark (deep/er/still) opens past them,
    # while side/a/b stays closed.
    assert '<details open><summary><span class="jt-key">still</span>' in html
    assert '<details><summary><span class="jt-key">b</span>' in html
    assert '<span class="jt-null">null</span>' in html and '<span class="jt-lit">3</span>' in html
