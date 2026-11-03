"""Tests for `commissure serve` and `commissure.serve`."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from commissure.api import count_external_resource_urls
from commissure.recipes import RecipeError, load_recipe
from commissure.runs import start_run
from commissure.serve import InterfaceApp, make_server
from commissure.workspace import lock, open_workspace

DEMO = Path(__file__).parent / "recipes" / "demo"

# A run's first parse spawns a worker whose imports can outlast the default 0.25s HTMX wait, which
# then returns the in-progress page. Tests asserting on the finished page wait for the run instead:
# the wait ends as soon as the run does, and tests of the in-progress page set their own short wait.
FINISHED_RUN_WAIT_SECONDS = 60.0


def _write_ui_recipe(folder: Path, *, extra_html: str = "") -> Path:
    """Write a minimal self-contained recipe defining `INTERFACE` using `commissure.api`."""
    tmpl_dir = folder / "templates"
    tmpl_dir.mkdir(parents=True, exist_ok=True)
    (tmpl_dir / "summary.html.j2").write_text(
        "<ul>{% for r in rows %}<li>{{ r.locator }}: {{ r.length }}</li>{% endfor %}</ul>\n",
        encoding="utf-8",
    )
    (folder / "recipe.py").write_text(
        f"""from __future__ import annotations

import html
from pathlib import Path
from pydantic import BaseModel, ConfigDict

from commissure.api import (
    ActionContext,
    ActionError,
    Group,
    Interface,
    InterfaceView,
    Item,
    RunContext,
    Stage,
    render_page,
)

EXTRA_HTML = {extra_html!r}
TEMPLATES = Path(__file__).resolve().parent / "templates"


class Extracted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locator: str
    length: int


class ReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_count: int
    relative_path: str


def extract(ctx: RunContext, item: Item) -> Extracted:
    return Extracted(locator=item.locator, length=len(item.text or ""))


def build_report(ctx: RunContext, group: Group) -> ReportOut:
    rows = [
        {{"locator": i.upstream["extract"].locator, "length": i.upstream["extract"].length}}
        for i in group.items
    ]
    rendered = render_page(
        template_dir=TEMPLATES,
        template_name="summary.html.j2",
        context={{"rows": rows}},
        title="Summary Report",
        outputs_dir=ctx.outputs,
    )
    (ctx.outputs / "summary.html").write_text(rendered, encoding="utf-8")
    return ReportOut(item_count=len(rows), relative_path="summary.html")


def render_ui(view: InterfaceView) -> str:
    links: list[str] = []
    for rec in view.records("report"):
        url = rec.output_url(rec.payload.relative_path)
        if url:
            state = "stale" if rec.stale else "ready"
            links.append(
                f\'<a class="report-link" data-state="{{state}}" href="{{html.escape(url)}}">\'
                f"Open Report ({{rec.payload.item_count}})</a>"
            )
    form = (
        \'<form method="post" action="/actions/add_batch">\'
        f\'<input type="hidden" name="csrf_token" value="{{html.escape(view.csrf_token)}}">\'
        \'<label for="batch">Batch folder</label>\'
        \'<input id="batch" name="batch" type="text" required>\'
        \'<button type="submit">Add Batch</button>\'
        "</form>"
    )
    body = "".join(links) or "<p>No reports built yet.</p>"
    return f"<section>{{body}}{{form}}{{EXTRA_HTML}}</section>"


def add_batch(ctx: ActionContext, form) -> str:
    batch = form.get("batch", "").strip()
    if not batch or "/" in batch or ".." in batch:
        raise ActionError("Invalid batch name.")
    target = ctx.solution_dir / "intake" / batch
    if not target.is_dir():
        raise ActionError(f"Batch {{batch}} does not exist.")
    ctx.ingest(target, role="corpus")
    ctx.run_pipeline()
    return f"Processed batch {{batch}}."


STAGES = [
    Stage("extract", extract, schema=Extracted, text="default"),
    Stage("report", build_report, schema=ReportOut, subject="run", needs=("extract",)),
]

INTERFACE = Interface(
    title="Test Operations Console",
    subtitle="Local management view",
    render=render_ui,
    actions={{"add_batch": add_batch}},
)
""",
        encoding="utf-8",
    )
    return folder


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@contextmanager
def _running_server(app: InterfaceApp) -> Iterator[str]:
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
        app.wait_for_idle(timeout=5)


def _http_request(
    url: str,
    *,
    method: str = "GET",
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
    req_headers = dict(headers or {})
    if data is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    opener = (
        urllib.request.build_opener()
        if follow_redirects
        else urllib.request.build_opener(_NoRedirect)
    )
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        try:
            body = err.read()
        finally:
            err.close()
        return err.code, {k.lower(): v for k, v in err.headers.items()}, body


def test_serve_cli_refuses_recipe_without_interface(cli, tmp_path):
    """AGENTS.md Section 1: core adds no web routes without a recipe's own INTERFACE."""
    ws_path = tmp_path / "ws"
    res = cli("serve", "--workspace", str(ws_path), "--recipe", str(DEMO))
    assert res.exit_code == 2
    assert "defines no INTERFACE" in res.stderr
    assert not ws_path.exists()


def test_serve_cli_requires_workspace_and_recipe_outside_solution_dir(cli, tmp_path, monkeypatch):
    monkeypatch.delenv("COMMISSURE_WORKSPACE", raising=False)
    monkeypatch.delenv("COMMISSURE_RECIPE", raising=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)

    r1 = cli("serve")
    assert r1.exit_code == 2
    assert "--workspace is required" in r1.stderr

    recipe_dir = _write_ui_recipe(tmp_path / "sol" / "recipe")
    r2 = cli("serve", "--recipe", str(recipe_dir))
    assert r2.exit_code == 2
    assert "--workspace is required" in r2.stderr

    r3 = cli("serve", "--workspace", str(tmp_path / "ws"))
    assert r3.exit_code == 2
    assert "--recipe is required" in r3.stderr
    assert not (tmp_path / "ws").exists()


@pytest.mark.parametrize(
    ("interface_expr", "expected_msg"),
    [
        ('"not an interface"', "not an Interface"),
        ('Interface(title="  ", render=lambda v: "<p>ok</p>")', "INTERFACE.title"),
        ('Interface(title="Valid", render="not_callable")', "INTERFACE.render"),
        (
            'Interface(title="Valid", render=lambda v: "", actions={"bad/name": lambda c, f: ""})',
            "INTERFACE action name",
        ),
        (
            'Interface(title="Valid", render=lambda v: "", actions={"run": "not_callable"})',
            "must be callable",
        ),
        (
            'Interface(title="Valid", subtitle=123, render=lambda v: "")',
            "INTERFACE.subtitle",
        ),
        (
            'Interface(title="Valid", footer=123, render=lambda v: "")',
            "INTERFACE.footer",
        ),
        (
            'Interface(title="Valid", brand_dir=True, render=lambda v: "")',
            "INTERFACE.brand_dir",
        ),
        (
            'Interface(title="Valid", brand_dir="missing_brand", render=lambda v: "")',
            "INTERFACE.brand_dir",
        ),
    ],
)
def test_invalid_interface_declarations_fail_at_load(tmp_path, interface_expr, expected_msg):
    folder = tmp_path / "bad_recipe"
    folder.mkdir()
    (folder / "recipe.py").write_text(
        f"""from pydantic import BaseModel, ConfigDict
from commissure.api import Interface, Stage

class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool = True

STAGES = [Stage("s", lambda ctx, i: Out(), schema=Out)]
INTERFACE = {interface_expr}
""",
        encoding="utf-8",
    )
    with pytest.raises(RecipeError, match=expected_msg):
        load_recipe(folder)


def test_get_uninitialized_workspace_is_readonly_and_offline(tmp_path):
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(sol / "recipe")
    ws_path = sol / "workspace"

    ui_app = InterfaceApp(ws_path, recipe_dir)
    assert not ws_path.exists()

    with _running_server(ui_app) as base_url:
        status, headers, raw = _http_request(f"{base_url}/")
        assert status == 200
        assert "text/html" in headers["content-type"]
        html_text = raw.decode("utf-8")
        assert "Test Operations Console" in html_text
        assert "No reports built yet." in html_text
        assert "Not yet initialized" in html_text
        assert 'aria-label="Idle"' in html_text
        assert 'aria-label="Steps"' in html_text
        assert count_external_resource_urls(html_text) == 0
        # Read-only GET must never create workspace/ or state.db.
        assert not ws_path.exists()

        for asset in ("favicon.svg", "pico.min.css", "report.css", "htmx.min.js", "report.js"):
            a_status, a_headers, a_body = _http_request(f"{base_url}/static/{asset}")
            assert a_status == 200, asset
            assert len(a_body) > 0
            assert a_headers.get("cache-control") == "no-cache"
            assert a_headers.get("x-content-type-options") == "nosniff"


def test_offline_gate_refuses_external_cdn_in_recipe_interface(tmp_path):
    """Prove the offline check fails when a recipe interface injects an external CDN URL."""
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(
        sol / "recipe",
        extra_html='<script src="https://cdn.example.com/tracker.js"></script>',
    )
    ui_app = InterfaceApp(sol / "workspace", recipe_dir)
    resp = ui_app.handle("GET", "/", {})
    assert resp.status == 500
    assert b"external resource URL" in resp.body


def test_csrf_and_output_path_confinement_checks_fail_on_bad_requests(tmp_path):
    """Prove CSRF protection and `/outputs/` confinement reject hostile requests."""
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(sol / "recipe")
    ws_path = sol / "workspace"
    secret_outside = tmp_path / "outside_secret.txt"
    secret_outside.write_text("top-secret-host-file", encoding="utf-8")

    ui_app = InterfaceApp(ws_path, recipe_dir)
    with _running_server(ui_app) as base_url:
        # 1. Missing CSRF token -> 403
        st_no_csrf, _, body_no_csrf = _http_request(
            f"{base_url}/actions/add_batch",
            method="POST",
            form={"batch": "b1"},
            follow_redirects=False,
        )
        assert st_no_csrf == 403
        assert b"CSRF" in body_no_csrf
        assert not ws_path.exists()

        # 2. Wrong CSRF token -> 403
        st_bad_csrf, _, _ = _http_request(
            f"{base_url}/_pipeline/run",
            method="POST",
            form={"csrf_token": "wrong-token", "step": "__all__"},
            follow_redirects=False,
        )
        assert st_bad_csrf == 403
        assert not ws_path.exists()

        # Initialize workspace outputs with one legitimate file and one escaping symlink.
        with lock(ws_path):
            ws = open_workspace(ws_path)
            legit = ws.outputs / "report" / "1" / "summary.html"
            legit.parent.mkdir(parents=True, exist_ok=True)
            legit.write_text("<!DOCTYPE html><title>ok</title>", encoding="utf-8")
            escape_link = ws.outputs / "report" / "1" / "escape.txt"
            escape_link.symlink_to(secret_outside)
            ws.close()

        st_ok, _, legit_body = _http_request(f"{base_url}/outputs/report/1/summary.html")
        assert st_ok == 200
        assert b"<title>ok</title>" in legit_body

        for attack in (
            "/outputs/../../outside_secret.txt",
            "/outputs/%2e%2e/%2e%2e/outside_secret.txt",
            "/outputs/report/1/escape.txt",
            "/static/../../outside_secret.txt",
        ):
            st_att, _, att_body = _http_request(f"{base_url}{attack}")
            assert st_att == 404, attack
            assert b"top-secret-host-file" not in att_body


def test_end_to_end_action_pipeline_cli_coexistence_and_lock(cli, tmp_path):
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(sol / "recipe")
    ws_path = sol / "workspace"
    batch1 = sol / "intake" / "batch_one"
    batch1.mkdir(parents=True)
    (batch1 / "a.json").write_text(json.dumps({"title": "alpha"}), encoding="utf-8")
    (batch1 / "b.json").write_text(json.dumps({"title": "beta"}), encoding="utf-8")

    ui_app = InterfaceApp(ws_path, recipe_dir, htmx_wait_seconds=FINISHED_RUN_WAIT_SECONDS)
    with _running_server(ui_app) as base_url:
        # Submit custom action via standard HTML POST -> 303 redirect -> GET /
        st_redirect, hdrs, _ = _http_request(
            f"{base_url}/actions/add_batch",
            method="POST",
            form={"csrf_token": ui_app.csrf_token, "batch": "batch_one"},
            follow_redirects=False,
        )
        assert st_redirect == 303
        assert hdrs["location"].startswith("/?notice=")

        st_home, _, home_bytes = _http_request(f"{base_url}{hdrs['location']}")
        assert st_home == 200
        home_html = home_bytes.decode("utf-8")
        assert "Processed batch batch_one." in home_html
        assert 'data-state="ready"' in home_html
        assert "Open Report (2)" in home_html
        assert "all current" in home_html
        assert count_external_resource_urls(home_html) == 0

        # Follow generated report link under /outputs/
        marker = 'href="/outputs/'
        start = home_html.index(marker) + len('href="')
        end = home_html.index('"', start)
        report_url = home_html[start:end]
        st_rep, _, rep_bytes = _http_request(f"{base_url}{report_url}")
        assert st_rep == 200
        rep_html = rep_bytes.decode("utf-8")
        assert "a.json:" in rep_html
        assert "b.json:" in rep_html
        assert count_external_resource_urls(rep_html) == 0

        # Coexistence with manual CLI run: ingest a second batch via `commissure run --step ingest`
        batch2 = sol / "intake" / "batch_two"
        batch2.mkdir(parents=True)
        (batch2 / "c.json").write_text(json.dumps({"title": "gamma"}), encoding="utf-8")
        code, ingest_rep = cli.ingest(ws_path, batch2)
        assert code == 0
        assert ingest_rep["added"] == 1

        # GET / immediately reflects pending parse + stale/pending downstream steps
        _, _, after_cli_bytes = _http_request(f"{base_url}/")
        after_cli_html = after_cli_bytes.decode("utf-8")
        assert "Next step" in after_cli_html
        assert "step(s) need attention" in after_cli_html

        # Run all steps in order via HTMX partial swap
        st_hx, _, hx_bytes = _http_request(
            f"{base_url}/_pipeline/run",
            method="POST",
            form={"csrf_token": ui_app.csrf_token, "step": "__all__"},
            headers={"HX-Request": "true"},
        )
        assert st_hx == 200
        hx_html = hx_bytes.decode("utf-8")
        assert hx_html.lstrip().startswith('<div id="interface-root">')
        assert "<!DOCTYPE html>" not in hx_html
        assert "Open Report (3)" in hx_html
        assert "all current" in hx_html

        # Prove workspace lock contention surfaces cleanly without crashing or corrupting state.db
        with lock(ws_path):
            st_locked_get, _, locked_get_bytes = _http_request(f"{base_url}/")
            assert st_locked_get == 200
            locked_html = locked_get_bytes.decode("utf-8")
            assert 'id="workspace-busy-bar"' in locked_html
            assert "header-icon-busy" in locked_html
            assert 'aria-label="Something else is running"' in locked_html
            assert "Workspace busy:" not in locked_html
            assert "Another process holds" not in locked_html
            assert (
                "Something else is still running. You can keep browsing, "
                "but you can't start a new action until it finishes."
            ) in locked_html

            st_locked_post, _, locked_post_bytes = _http_request(
                f"{base_url}/_pipeline/run",
                method="POST",
                form={"csrf_token": ui_app.csrf_token, "step": "report"},
                headers={"HX-Request": "true"},
            )
            assert st_locked_post == 200
            locked_post_html = locked_post_bytes.decode("utf-8")
            assert "is locked by another process" in locked_post_html
            assert 'role="alert"' in locked_post_html


def test_default_parse_all_quarantined_fails_closed(tmp_path):
    """Prove default `parse` still fails closed when 100% of ingested items are quarantined."""
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(sol / "recipe")
    ws_path = sol / "workspace"
    broken_batch = sol / "intake" / "broken"
    broken_batch.mkdir(parents=True)
    (broken_batch / "corrupt.json").write_text("{not valid json", encoding="utf-8")

    ui_app = InterfaceApp(ws_path, recipe_dir, htmx_wait_seconds=FINISHED_RUN_WAIT_SECONDS)
    resp = ui_app.handle(
        "POST",
        "/actions/add_batch",
        {"content-type": "application/x-www-form-urlencoded", "hx-request": "true"},
        urllib.parse.urlencode({"csrf_token": ui_app.csrf_token, "batch": "broken"}).encode(),
    )
    assert resp.status == 200
    html_out = resp.body.decode("utf-8")
    assert 'role="alert"' in html_out
    assert "step parse failed: AllQuarantined" in html_out


def test_async_htmx_busy_bar_progress_polling_and_completion(tmp_path):
    """Prove sticky busy bar, determinate ledger progress, output browsing, and idle stop."""
    fetch_entered = threading.Event()
    release_fetch = threading.Event()
    stage_midway = threading.Event()
    release_stage = threading.Event()

    # Expose synchronization events via a module-level registry keyed by workspace path
    # so the dynamically loaded recipe module can coordinate deterministically without sleeps.
    import sys

    coord_key = f"_test_serve_coord_{id(fetch_entered)}"
    sys.modules[coord_key] = type(  # type: ignore[assignment]
        "_Coord",
        (),
        {
            "fetch_entered": fetch_entered,
            "release_fetch": release_fetch,
            "stage_midway": stage_midway,
            "release_stage": release_stage,
        },
    )()

    try:
        sol = tmp_path / "solution"
        recipe_dir = sol / "recipe"
        tmpl_dir = recipe_dir / "templates"
        tmpl_dir.mkdir(parents=True, exist_ok=True)
        (tmpl_dir / "summary.html.j2").write_text(
            "<ul>{% for r in rows %}<li>{{ r.locator }}</li>{% endfor %}</ul>\n",
            encoding="utf-8",
        )
        (recipe_dir / "recipe.py").write_text(
            f"""from __future__ import annotations
import html
import sys
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from commissure.api import (
    ActionContext, Group, Interface, InterfaceView, Item, RunContext, Stage, render_page
)

COORD = sys.modules[{coord_key!r}]
TEMPLATES = Path(__file__).resolve().parent / "templates"

class Extracted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locator: str

class ReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_count: int
    relative_path: str

def extract(ctx: RunContext, item: Item) -> Extracted:
    if item.locator == "b.json" and not COORD.release_stage.is_set():
        COORD.stage_midway.set()
        assert COORD.release_stage.wait(timeout=10)
    return Extracted(locator=item.locator)

def build_report(ctx: RunContext, group: Group) -> ReportOut:
    rows = [{{"locator": i.upstream["extract"].locator}} for i in group.items]
    rendered = render_page(
        template_dir=TEMPLATES,
        template_name="summary.html.j2",
        context={{"rows": rows}},
        title="Summary Report",
        outputs_dir=ctx.outputs,
    )
    (ctx.outputs / "summary.html").write_text(rendered, encoding="utf-8")
    return ReportOut(item_count=len(rows), relative_path="summary.html")

def render_ui(view: InterfaceView) -> str:
    links = []
    for rec in view.records("report"):
        url = rec.output_url(rec.payload.relative_path)
        if url:
            links.append(
                f'<a class="report-link" href="{{html.escape(url)}}">'
                f"Open Report ({{rec.payload.item_count}})</a>"
            )
    dis = " disabled" if view.locked else ""
    form = (
        '<form method="post" action="/actions/add_slow">'
        f'<input type="hidden" name="csrf_token" value="{{html.escape(view.csrf_token)}}">'
        f'<button type="submit"{{dis}}>Run Slow Batch</button>'
        "</form>"
    )
    return "<section>" + "".join(links) + form + "</section>"

def add_fast(ctx: ActionContext, form) -> str:
    target = ctx.solution_dir / "intake" / "seed"
    ctx.ingest(target, role="corpus")
    ctx.run_pipeline()
    return "Seeded initial report."

def add_slow(ctx: ActionContext, form) -> str:
    ctx.workers = 1  # sequential subject execution so 2/4 committed count is deterministic
    ctx.set_status("Downloading upstream batch slow1")
    COORD.fetch_entered.set()
    assert COORD.release_fetch.wait(timeout=10)
    target = ctx.solution_dir / "intake" / "slow1"
    ctx.ingest(target, role="corpus")
    ctx.run_pipeline()
    return "Processed slow batch slow1."

STAGES = [
    Stage("extract", extract, schema=Extracted, text="default"),
    Stage("report", build_report, schema=ReportOut, subject="run", needs=("extract",)),
]

INTERFACE = Interface(
    title="Test Operations Console",
    render=render_ui,
    actions={{"add_fast": add_fast, "add_slow": add_slow}},
)
""",
            encoding="utf-8",
        )

        seed = sol / "intake" / "seed"
        seed.mkdir(parents=True)
        (seed / "seed.json").write_text('{"ok": true}', encoding="utf-8")

        slow_dir = sol / "intake" / "slow1"
        slow_dir.mkdir(parents=True)
        for name in ("a.json", "b.json", "c.json"):
            (slow_dir / name).write_text(json.dumps({"name": name}), encoding="utf-8")

        ws_path = sol / "workspace"
        ui_app = InterfaceApp(ws_path, recipe_dir, htmx_wait_seconds=0.02)

        with _running_server(ui_app) as base_url:
            # 0. Seed an existing completed report so outputs stay browseable while busy.
            st_seed, _, _ = _http_request(
                f"{base_url}/actions/add_fast",
                method="POST",
                form={"csrf_token": ui_app.csrf_token},
            )
            assert st_seed == 200
            _, _, idle_bytes = _http_request(f"{base_url}/")
            idle_html = idle_bytes.decode("utf-8")
            assert 'data-busy="false"' in idle_html
            assert 'id="workspace-busy-bar"' not in idle_html
            assert 'hx-trigger="every 5s"' not in idle_html
            marker = 'href="/outputs/'
            start = idle_html.index(marker) + len('href="')
            existing_report_url = idle_html[start : idle_html.index('"', start)]

            # 1. Trigger slow action via HTMX POST -> returns immediately in busy mode.
            st_post, _, busy_bytes = _http_request(
                f"{base_url}/actions/add_slow",
                method="POST",
                form={"csrf_token": ui_app.csrf_token},
                headers={"HX-Request": "true"},
            )
            assert fetch_entered.wait(timeout=5)
            assert st_post == 200
            busy_html = busy_bytes.decode("utf-8")
            assert 'data-busy="true"' in busy_html
            assert 'id="workspace-busy-bar"' in busy_html
            assert 'hx-get="/_status"' in busy_html
            assert 'hx-trigger="every 5s"' in busy_html
            assert "Workspace busy:" not in busy_html
            assert "header-icon-busy" in busy_html
            assert "header-busy-rail" in busy_html
            assert "Downloading upstream batch slow1" in busy_html
            assert (
                "You can keep browsing. You can't start another action until this one finishes."
            ) in busy_html
            # Pre-DB phase has no item count -> indeterminate `<progress>` (no `value=` attribute).
            assert 'class="busy-progress"' in busy_html
            assert "value=" not in busy_html.split("<progress", 1)[1].split(">", 1)[0]
            expected_busy_title = (
                'data-page-title="[Running: Downloading upstream batch slow1]'
                ' Test Operations Console"'
            )
            assert expected_busy_title in busy_html
            # Form buttons are disabled, while completed report link stays clickable & serves 200!
            assert ">Run Slow Batch</button>" in busy_html
            assert "disabled>Run Slow Batch</button>" in busy_html
            assert f'href="{existing_report_url}"' in busy_html
            st_rep_while_busy, _, rep_body = _http_request(f"{base_url}{existing_report_url}")
            assert st_rep_while_busy == 200
            assert b"seed.json" in rep_body

            # Second browser tab hitting GET / sees the exact same `[Running: ...]` <title> and bar.
            _, _, tab2_bytes = _http_request(f"{base_url}/")
            tab2_html = tab2_bytes.decode("utf-8")
            assert (
                "<title>[Running: Downloading upstream batch slow1] Test Operations Console</title>"
                in tab2_html
            )
            assert 'id="workspace-busy-bar"' in tab2_html
            assert count_external_resource_urls(tab2_html) == 0

            # 2. Advance past fetch into `extract` stage (seed.json + a.json commit before b.json).
            release_fetch.set()
            assert stage_midway.wait(timeout=5)

            st_poll, poll_hdrs, poll_bytes = _http_request(f"{base_url}/_status")
            assert st_poll == 200
            assert "hx-retarget" not in poll_hdrs
            poll_html = poll_bytes.decode("utf-8")
            assert count_external_resource_urls(poll_html) == 0
            assert "Running: extract" in poll_html
            assert "Running step:" not in poll_html
            # Snapshot stage `extract` has 4 subjects (seed.json, a.json, b.json, c.json);
            # seed.json and a.json have committed (2 / 4 = 50%) while b.json is blocked!
            assert "2 / 4 items (50%)" in poll_html
            assert 'value="2" max="4"' in poll_html
            assert 'data-page-title="[Running: extract 2/4] Test Operations Console"' in poll_html

            # 3. Release stage and poll `/_status` -> retargets `#interface-root` and stops polling.
            release_stage.set()
            assert ui_app.wait_for_idle(timeout=5)

            st_done, done_hdrs, done_bytes = _http_request(f"{base_url}/_status")
            assert st_done == 200
            assert done_hdrs.get("hx-retarget") == "#interface-root"
            assert done_hdrs.get("hx-reswap") == "outerHTML"
            done_html = done_bytes.decode("utf-8")
            assert count_external_resource_urls(done_html) == 0
            assert 'data-busy="false"' in done_html
            assert 'id="workspace-busy-bar"' not in done_html
            assert 'hx-trigger="every 5s"' not in done_html
            assert "Processed slow batch slow1." in done_html
            assert "Open Report (4)" in done_html
            assert 'data-page-title="Test Operations Console"' in done_html
            assert "disabled>Run Slow Batch</button>" not in done_html
    finally:
        release_fetch.set()
        release_stage.set()
        sys.modules.pop(coord_key, None)


def test_abandoned_running_row_without_lock_does_not_falsely_lock_and_warns(tmp_path):
    """Prove a crashed CLI process (`status='running'`, lock free) never causes an endless lock."""
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(sol / "recipe")
    ws_path = sol / "workspace"

    with lock(ws_path):
        ws = open_workspace(ws_path)
        try:
            start_run(ws.conn, "recipe.extract", trigger="cli", config={})
        finally:
            ws.close()

    ui_app = InterfaceApp(ws_path, recipe_dir)
    resp = ui_app.handle("GET", "/", {})
    assert resp.status == 200
    html_out = resp.body.decode("utf-8")
    assert 'data-busy="false"' in html_out
    assert 'id="workspace-busy-bar"' not in html_out
    assert 'aria-label="Needs attention"' in html_out
    assert "header-icon-attention" in html_out
    assert "Interrupted: extract" not in html_out
    assert "Interrupted run detected:" not in html_out
    assert "Interrupted run:" in html_out
    assert "<code>extract</code>" in html_out
    assert "Run this step again to pick up where it left off." in html_out
    assert count_external_resource_urls(html_out) == 0


def test_core_shell_header_info_disclosure_drawers_and_default_footer(tmp_path):
    """Prove 3-zone Core Shell layout: sticky header controls, info popover, and default footer."""
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(sol / "recipe")
    ws_path = sol / "workspace"
    batch = sol / "intake" / "b1"
    batch.mkdir(parents=True)
    (batch / "a.json").write_text('{"ok": true}', encoding="utf-8")

    ui_app = InterfaceApp(ws_path, recipe_dir, htmx_wait_seconds=FINISHED_RUN_WAIT_SECONDS)
    with _running_server(ui_app) as base_url:
        _, _, init_bytes = _http_request(f"{base_url}/")
        init_html = init_bytes.decode("utf-8")
        assert count_external_resource_urls(init_html) == 0

        # 1. Header contains single <h1>, info disclosure (not <p class="lede">), quiet icons.
        header_part, rest = init_html.split("</header>", 1)
        main_part, footer_part = rest.split("</main>", 1)
        assert '<header class="app-header">' in header_part
        assert '<h1 class="app-title">Test Operations Console</h1>' in header_part
        assert "app-brand-slot" not in header_part
        assert 'id="app-info-disclosure"' in header_part
        assert 'aria-label="About Test Operations Console"' in header_part
        assert "Local management view" in header_part
        assert 'class="lede"' not in init_html
        assert "Local management view" not in main_part
        assert 'id="idle-status-details"' in header_part
        assert 'aria-label="Idle"' in header_part
        assert "Workspace idle" not in header_part
        assert 'id="pipeline-drawer"' in header_part
        assert 'aria-label="Steps"' in header_part
        assert "Pipeline: not initialized" not in header_part
        assert "Not yet initialized" in header_part
        # Steps table lives in the header drawer, not at the bottom of <main>.
        assert "Run All Steps in Order" in header_part
        assert "Run All Steps in Order" not in main_part

        # 2. Default Commissure footer renders in centered app-footer.
        assert '<footer class="site-footer app-footer">' in footer_part
        assert "&copy; Commissure" in footer_part

        # 3. Run batch -> steps icon stays quiet (`Steps`); static report stays footer-free.
        st_post, _, run_bytes = _http_request(
            f"{base_url}/actions/add_batch",
            method="POST",
            form={"csrf_token": ui_app.csrf_token, "batch": "b1"},
            headers={"HX-Request": "true"},
        )
        assert st_post == 200
        run_html = run_bytes.decode("utf-8")
        assert 'aria-label="Steps"' in run_html
        assert "Steps: all current" in run_html
        assert "Pipeline: all current" not in run_html
        # Action run (`open_pipeline=False`) leaves steps drawer closed.
        drawer_tag = run_html.split('id="pipeline-drawer"', 1)[1].split(">", 1)[0]
        assert " open" not in drawer_tag

        marker = 'href="/outputs/'
        start = run_html.index(marker) + len('href="')
        report_url = run_html[start : run_html.index('"', start)]
        _, _, static_rep_bytes = _http_request(f"{base_url}{report_url}")
        static_rep_html = static_rep_bytes.decode("utf-8")
        assert '<header class="app-header">' in static_rep_html
        assert '<footer class="site-footer app-footer">' in static_rep_html
        assert 'class="header-home-link" href="/"' in static_rep_html
        assert "Test Operations Console" in static_rep_html

        # 4. Failed step marks steps as needing attention and leaves the drawer closed.
        bad = sol / "intake" / "bad"
        bad.mkdir(parents=True)
        (bad / "corrupt.json").write_text("{broken", encoding="utf-8")
        ws_bad = tmp_path / "ws_bad"
        bad_app = InterfaceApp(ws_bad, recipe_dir, htmx_wait_seconds=FINISHED_RUN_WAIT_SECONDS)
        fail_resp = bad_app.handle(
            "POST",
            "/actions/add_batch",
            {"content-type": "application/x-www-form-urlencoded", "hx-request": "true"},
            urllib.parse.urlencode({"csrf_token": bad_app.csrf_token, "batch": "bad"}).encode(),
        )
        fail_html = fail_resp.body.decode("utf-8")
        assert 'aria-label="Steps need attention"' in fail_html
        assert "header-icon-attention" in fail_html
        assert "Steps: 1 failed" in fail_html
        assert "Pipeline: 1 failed" not in fail_html
        fail_drawer_tag = fail_html.split('id="pipeline-drawer"', 1)[1].split(">", 1)[0]
        assert " open" not in fail_drawer_tag


def test_core_shell_auto_discovers_recipe_brand_and_footer_overrides(tmp_path):
    """Prove auto-discovered `recipe/brand/`, static logo serving, and `Interface.footer`."""
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(sol / "recipe")
    ws_path = sol / "workspace"

    # 1. When subtitle is empty, `#app-info-disclosure` is omitted completely.
    recipe_py = recipe_dir / "recipe.py"
    orig_code = recipe_py.read_text(encoding="utf-8")
    recipe_py.write_text(
        orig_code.replace('subtitle="Local management view",', 'subtitle="",'),
        encoding="utf-8",
    )
    ui_app = InterfaceApp(ws_path, recipe_dir)
    no_sub_html = ui_app.handle("GET", "/", {}).body.decode("utf-8")
    assert 'id="app-info-disclosure"' not in no_sub_html

    # 2. Drop `recipe/brand/{header.html,footer.html,brand.css,logo.svg}` without setting brand_dir.
    brand = recipe_dir / "brand"
    brand.mkdir()
    (brand / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<rect width="10" height="10"/></svg>',
        encoding="utf-8",
    )
    (brand / "header.html").write_text(
        '<a class="org-brand" href="/"><img src="/static/brand/logo.svg" alt="Acme Agency"></a>',
        encoding="utf-8",
    )
    (brand / "footer.html").write_text(
        "<p>Prepared for Acme Agency under Compliance Policy 42.</p>",
        encoding="utf-8",
    )
    (brand / "brand.css").write_text(
        ":root { --brand-header-bg: #162e51; --brand-accent: #205493; }\n",
        encoding="utf-8",
    )

    branded_html = ui_app.handle("GET", "/", {}).body.decode("utf-8")
    assert count_external_resource_urls(branded_html) == 0
    assert '<link rel="stylesheet" href="/static/brand/brand.css">' in branded_html
    assert '<div class="app-brand-slot">' in branded_html
    assert 'alt="Acme Agency"' in branded_html
    assert "Prepared for Acme Agency under Compliance Policy 42." in branded_html
    assert "&copy; Commissure" not in branded_html

    logo_resp = ui_app.handle("GET", "/static/brand/logo.svg", {})
    assert logo_resp.status == 200
    assert "image/svg+xml" in logo_resp.headers["Content-Type"]
    # Inline HTML fragments are never exposed as downloadable static files.
    assert ui_app.handle("GET", "/static/brand/header.html", {}).status == 404
    assert ui_app.handle("GET", "/static/brand/footer.html", {}).status == 404

    # Prove offline gate still refuses an external image URL injected via `brand/header.html`!
    (brand / "header.html").write_text(
        '<img src="https://cdn.example.com/remote-logo.svg" alt="Bad">',
        encoding="utf-8",
    )
    bad_brand_resp = ui_app.handle("GET", "/", {})
    assert bad_brand_resp.status == 500
    assert b"external resource URL" in bad_brand_resp.body
    (brand / "header.html").write_text(
        '<a class="org-brand" href="/"><img src="/static/brand/logo.svg" alt="Acme Agency"></a>',
        encoding="utf-8",
    )

    # 3. `Interface(..., footer=...)` escapes HTML and overrides `brand/footer.html`.
    recipe_py.write_text(
        orig_code.replace(
            'subtitle="Local management view",',
            'subtitle="Local management view",\n'
            '    footer="Client Notice <v1>\\nAuthorized Use Only",',
        ),
        encoding="utf-8",
    )
    py_footer_html = ui_app.handle("GET", "/", {}).body.decode("utf-8")
    assert "<p>Client Notice &lt;v1&gt;<br>Authorized Use Only</p>" in py_footer_html
    assert "Compliance Policy 42" not in py_footer_html

    # 4. `Interface(..., brand_dir=False)` disables auto-discovered `recipe/brand/`.
    recipe_py.write_text(
        orig_code.replace(
            'subtitle="Local management view",',
            'subtitle="Local management view",\n    brand_dir=False,',
        ),
        encoding="utf-8",
    )
    unbranded_html = ui_app.handle("GET", "/", {}).body.decode("utf-8")
    assert "app-brand-slot" not in unbranded_html
    assert "/static/brand/brand.css" not in unbranded_html
    assert "&copy; Commissure" in unbranded_html
    assert ui_app.handle("GET", "/static/brand/logo.svg", {}).status == 404


def test_served_html_outputs_have_home_nav_version_switcher_and_scope_drawer(tmp_path):
    """Prove served deliverables get home link and snapshot history while disk stays pure."""
    sol = tmp_path / "solution"
    recipe_dir = _write_ui_recipe(sol / "recipe")
    ws_path = sol / "workspace"

    b1 = sol / "intake" / "b1"
    b1.mkdir(parents=True)
    (b1 / "first.json").write_text('{"n": 1}', encoding="utf-8")

    b2 = sol / "intake" / "b2"
    b2.mkdir(parents=True)
    (b2 / "second.json").write_text('{"n": 2}', encoding="utf-8")

    ui_app = InterfaceApp(ws_path, recipe_dir, htmx_wait_seconds=FINISHED_RUN_WAIT_SECONDS)
    with _running_server(ui_app) as base_url:
        # Run 1: single batch -> produces initial summary.html
        st1, _, html1_bytes = _http_request(
            f"{base_url}/actions/add_batch",
            method="POST",
            form={"csrf_token": ui_app.csrf_token, "batch": "b1"},
            headers={"HX-Request": "true"},
        )
        assert st1 == 200
        html1 = html1_bytes.decode("utf-8")
        assert "Workspace Pipeline Steps" in html1
        assert "1 active item(s) across 1 source(s)" in html1
        assert '<th scope="col">Scope</th>' in html1
        m1 = 'href="/outputs/'
        s1 = html1.index(m1) + len('href="')
        url_run1 = html1[s1 : html1.index('"', s1)]

        # Run 2: second batch -> produces second summary.html snapshot
        st2, _, html2_bytes = _http_request(
            f"{base_url}/actions/add_batch",
            method="POST",
            form={"csrf_token": ui_app.csrf_token, "batch": "b2"},
            headers={"HX-Request": "true"},
        )
        assert st2 == 200
        html2 = html2_bytes.decode("utf-8")
        assert "2 active item(s) across 2 source(s)" in html2
        s2 = html2.index(m1) + len('href="')
        url_run2 = html2[s2 : html2.index('"', s2)]
        assert url_run2 != url_run1

        # Latest report served over HTTP has home link + snapshot switcher listing older run.
        st_latest, _, latest_bytes = _http_request(f"{base_url}{url_run2}")
        assert st_latest == 200
        latest_html = latest_bytes.decode("utf-8")
        assert count_external_resource_urls(latest_html) == 0
        assert 'class="header-home-link" href="/"' in latest_html
        assert "Test Operations Console" in latest_html
        assert 'id="snapshot-versions-details"' in latest_html
        assert f'href="{url_run1}"' in latest_html
        assert "Viewing historical snapshot" not in latest_html
        assert "first.json" in latest_html and "second.json" in latest_html

        # File on disk remains pure offline HTML with no server-only `href="/"` link.
        rel_disk = url_run2.removeprefix("/outputs/")
        disk_html = (ws_path / "outputs" / rel_disk).read_text(encoding="utf-8")
        assert 'class="header-home-link"' not in disk_html
        assert 'id="snapshot-versions-details"' not in disk_html

        # Navigating to the older snapshot renders older data + banner linking back to latest.
        st_old, _, old_bytes = _http_request(f"{base_url}{url_run1}")
        assert st_old == 200
        old_html = old_bytes.decode("utf-8")
        assert count_external_resource_urls(old_html) == 0
        assert "first.json" in old_html and "second.json" not in old_html
        assert "Viewing historical snapshot" in old_html
        assert f'href="{url_run2}"' in old_html

        # HTML without the `render_page` shell slots is served exactly as written: core does
        # not retrofit headers, stylesheets, or footers into arbitrary output files.
        plain = (ws_path / "outputs" / rel_disk).with_name("plain.html")
        plain_bytes = (
            b'<html><head><link rel="stylesheet" href="report.css"></head>'
            b"<body><main>raw</main></body></html>"
        )
        plain.write_bytes(plain_bytes)
        st_plain, _, served_plain = _http_request(
            f"{base_url}{url_run2.rsplit('/', 1)[0]}/plain.html"
        )
        assert st_plain == 200
        assert served_plain == plain_bytes


def test_serve_local_llm_uses_one_worker_shows_llm_badge_and_reports_checkpoint_progress(tmp_path):
    sol = tmp_path / "solution"
    recipe_dir = sol / "recipe"
    recipe_dir.mkdir(parents=True)
    ws_path = sol / "workspace"
    batch = sol / "intake" / "b1"
    batch.mkdir(parents=True)
    (batch / "a.json").write_text('{"n": 1}', encoding="utf-8")
    (batch / "b.json").write_text('{"n": 2}', encoding="utf-8")

    (recipe_dir / "recipe.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import ActionContext, Interface, InterfaceView, Stage\n\n"
        "class Out(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    workers: int\n\n"
        "def llm_stage(ctx, item):\n"
        "    if item.locator == 'b.json':\n"
        "        raise RuntimeError('simulated mid-run timeout')\n"
        "    return Out(workers=ctx.workers)\n\n"
        "def ingest_only(ctx: ActionContext, form) -> str:\n"
        "    ctx.ingest(ctx.solution_dir / 'intake' / 'b1', role='corpus')\n"
        "    ctx.run_step('parse')\n"
        "    return 'Ingested.'\n\n"
        "def render_ui(view: InterfaceView) -> str:\n"
        "    return '<p>UI <a href=\"https://www.regulations.gov\">Reg.gov</a></p>'\n\n"
        "STAGES = [\n"
        "    Stage('stance', llm_stage, schema=Out, text='default',\n"
        "          params={'model': 'llamacpp:local-gguf', 'base_url': 'http://localhost:9931/v1',"
        " 'context_tokens': 8192}),\n"
        "]\n"
        "INTERFACE = Interface(\n"
        "    title='LLM Console',\n"
        "    render=render_ui,\n"
        "    actions={'setup': ingest_only},\n"
        ")\n",
        encoding="utf-8",
    )

    ui_app = InterfaceApp(ws_path, recipe_dir, htmx_wait_seconds=5.0)
    # Uninitialized view already marks `stance` with uses_llm=True and renders the LLM badge.
    init_html = ui_app.handle("GET", "/", {}).body.decode("utf-8")
    assert '<span class="badge">LLM</span>' in init_html
    assert (
        '<a href="https://www.regulations.gov" target="_blank" rel="noopener noreferrer">'
        "Reg.gov</a>"
    ) in init_html

    setup_resp = ui_app.handle(
        "POST",
        "/actions/setup",
        {"content-type": "application/x-www-form-urlencoded", "hx-request": "true"},
        urllib.parse.urlencode({"csrf_token": ui_app.csrf_token}).encode(),
    )
    assert setup_resp.status == 200
    assert '<span class="badge">LLM</span>' in setup_resp.body.decode("utf-8")

    run_resp = ui_app.handle(
        "POST",
        "/_pipeline/run",
        {"content-type": "application/x-www-form-urlencoded", "hx-request": "true"},
        urllib.parse.urlencode({"csrf_token": ui_app.csrf_token, "step": "stance"}).encode(),
    )
    assert run_resp.status == 200
    err_html = run_resp.body.decode("utf-8")
    assert "step stance failed: RuntimeError: simulated mid-run timeout" in err_html
    assert "1 completed, 1 remaining" in err_html

    opened = open_workspace(ws_path)
    try:
        rec = json.loads(
            opened.conn.execute("SELECT payload FROM records ORDER BY id DESC LIMIT 1").fetchone()[
                0
            ]
        )
    finally:
        opened.close()
    assert rec["workers"] == 1


def test_set_status_without_counts_clears_previous_phase_counts(tmp_path):
    """A new phase label without counts must not keep showing the last phase's done/total."""
    sol = tmp_path / "solution"
    ui_app = InterfaceApp(sol / "workspace", _write_ui_recipe(sol / "recipe"))
    ui_app._active_started_mono = time.monotonic()
    ui_app._set_active_label("Fetching comments", done=975, total=975)
    assert ui_app._snapshot_in_process_activity()[3:] == (975, 975)
    ui_app._set_active_label("Running: ingest (comment)")
    _busy, label, _elapsed, done, total = ui_app._snapshot_in_process_activity()
    assert label == "Running: ingest (comment)"
    assert (done, total) == (None, None)
