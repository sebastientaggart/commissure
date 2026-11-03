"""Global resource limits, sparse-file guards, child-process parse timeouts, and serve shutdown."""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

import pypdfium2
import pytest
from openpyxl import Workbook

import commissure.serve as serve_mod
from commissure.serve import InterfaceApp
from commissure.workspace import DB_NAME, connect, open_workspace

DEMO = Path(__file__).parent / "recipes" / "demo"


def _query(ws_root: Path, sql: str, *params: object) -> list[dict]:
    conn = connect(ws_root / DB_NAME)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _write_text_pdf(path: Path, page_count: int) -> None:
    """Build a minimal valid multi-page PDF with readable Helvetica text on each page."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    pages_id = add(b"")  # placeholder filled once kid ids exist
    kids: list[int] = []
    for n in range(1, page_count + 1):
        stream = f"BT /F1 12 Tf 72 720 Td (Page {n} content text) Tj ET".encode("ascii")
        content_id = add(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
        page_id = add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode("ascii")
        )
        kids.append(page_id)
    kids_ref = " ".join(f"{k} 0 R" for k in kids)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids_ref}] /Count {page_count} >>".encode(
        "ascii"
    )
    catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))

    buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf.extend(f"{idx} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(bytes(buf))
    # Sanity-check PDFium agrees on page count.
    doc = pypdfium2.PdfDocument(path)
    try:
        assert len(doc) == page_count
    finally:
        doc.close()


def _write_ui_recipe(folder: Path, extra_recipe_code: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "recipe.py").write_text(
        f"""from pydantic import BaseModel, ConfigDict
from commissure.api import Interface, Stage

class ItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locator: str

def extract_fn(ctx, item):
    return ItemOut(locator=item.locator)

def render_ui(view):
    return f"<section><p>Items: {{sum(view.role_counts.values())}}</p></section>"

{extra_recipe_code}

STAGES = [Stage("extract", extract_fn, schema=ItemOut, text="default", allow_empty=True)]
INTERFACE = Interface(title="Resilience Test UI", render=render_ui)
""",
        encoding="utf-8",
    )
    return folder


def test_sparse_xlsx_collapses_empty_row_void_with_warning(cli, ws_root, tmp_path):
    """A stray cell far down an XLSX sheet collapses the empty void and emits a warning."""
    src = tmp_path / "src"
    src.mkdir()

    # 1. Sparse sheet: rows 1..2 have data, and row 50,000 has a stray paste.
    wb_sparse = Workbook()
    ws_s = wb_sparse.active
    ws_s.title = "Facilities"
    ws_s["A1"] = "Facility"
    ws_s["B1"] = "Status"
    ws_s["A2"] = "Plant Alpha"
    ws_s["B2"] = "Active"
    ws_s["D50000"] = "Not on Federal land"
    wb_sparse.save(src / "sparse.xlsx")

    # 2. Normal sheet with a small 3-row internal blank gap stays untouched and valid.
    wb_normal = Workbook()
    ws_n = wb_normal.active
    ws_n["A1"] = "Top"
    ws_n["A5"] = "Bottom"
    wb_normal.save(src / "normal.xlsx")

    p_res = cli("probe", str(src), "--json")
    assert p_res.exit_code == 0, p_res.output
    probe_files = {f["locator"]: f for f in json.loads(p_res.stdout)["files"]}
    sparse_sheet_probe = probe_files["sparse.xlsx"]["xlsx"]["sheets"][0]
    assert sparse_sheet_probe["rows"] == 50000
    assert sparse_sheet_probe["data_rows"] == 2
    assert sparse_sheet_probe["empty_rows"] == 49997

    code, _ = cli.ingest(ws_root, src)
    assert code == 0
    r_parse = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json")
    assert r_parse.exit_code == 0, r_parse.output
    report = json.loads(r_parse.stdout)
    assert report["valid"] == 1
    assert report["warning"] == 1

    rows = _query(
        ws_root,
        "SELECT i.locator, o.outcome, o.reason_code, o.diagnostics, e.text, e.structure"
        " FROM outcomes o JOIN item_versions v ON v.id = o.item_version_id"
        " JOIN items i ON i.id = v.item_id"
        " JOIN extracted_text e ON e.id = o.extracted_text_id"
        " ORDER BY i.locator",
    )
    by_loc = {r["locator"]: r for r in rows}

    assert by_loc["normal.xlsx"]["outcome"] == "valid"
    normal_struct = json.loads(by_loc["normal.xlsx"]["structure"])
    assert len(normal_struct["tables"][0]["rows"]) == 5

    sparse_row = by_loc["sparse.xlsx"]
    assert sparse_row["outcome"] == "warning"
    assert sparse_row["reason_code"] == "sparse_rows_collapsed"
    assert "Plant Alpha" in sparse_row["text"]
    assert "Not on Federal land" in sparse_row["text"]
    assert len(sparse_row["text"]) + len(sparse_row["structure"]) < 8_000
    sparse_struct = json.loads(sparse_row["structure"])
    # 2 top rows + 1 separator blank row + 1 stray bottom row = 4 retained rows (not 50,000).
    assert len(sparse_struct["tables"][0]["rows"]) == 4
    diags = json.loads(sparse_row["diagnostics"])
    assert diags[0]["code"] == "sparse_rows_collapsed"
    assert diags[0]["detail"]["total_collapsed_rows"] == 49996


def test_source_discovery_limits_fail_ingest_and_probe_fast(cli, ws_root, tmp_path):
    """Exceeding max_source_files or max_source_bytes fails fast before copying blobs."""
    src = tmp_path / "src"
    src.mkdir()
    for idx in range(5):
        (src / f"f{idx}.json").write_text(
            json.dumps({"i": idx, "pad": "x" * 200}), encoding="utf-8"
        )

    r_files = cli(
        "run",
        "--step",
        "ingest",
        "--workspace",
        str(ws_root),
        "--source",
        str(src),
        "--json",
        env={"COMMISSURE_MAX_SOURCE_FILES": "3"},
    )
    assert r_files.exit_code == 1
    rep_files = json.loads(r_files.stdout)
    assert rep_files["status"] == "failed"
    assert "max_source_files" in rep_files["error"]
    blobs_dir = ws_root / "blobs"
    assert not any(p.is_file() for p in blobs_dir.rglob("*"))

    r_probe = cli("probe", str(src), env={"COMMISSURE_MAX_SOURCE_FILES": "3"})
    assert r_probe.exit_code == 2
    assert "max_source_files" in r_probe.stderr

    r_bytes = cli(
        "run",
        "--step",
        "ingest",
        "--workspace",
        str(ws_root),
        "--source",
        str(src),
        "--json",
        env={"COMMISSURE_MAX_SOURCE_BYTES": "400"},
    )
    assert r_bytes.exit_code == 1
    rep_bytes = json.loads(r_bytes.stdout)
    assert rep_bytes["status"] == "failed"
    assert "max_source_bytes" in rep_bytes["error"]
    assert not any(p.is_file() for p in blobs_dir.rglob("*"))


def test_item_limits_quarantine_and_retry_when_limit_raised(cli, ws_root, tmp_path):
    """Per-item limits quarantine only the outlier; raising the limit retries only that item."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "small.csv").write_text("col1,col2\n1,2\n3,4\n", encoding="utf-8")
    (src / "tall.csv").write_text(
        "col1,col2\n" + "".join(f"{i},{i * 2}\n" for i in range(10)),
        encoding="utf-8",
    )
    (src / "huge.json").write_text(json.dumps({"blob": "z" * 1200}), encoding="utf-8")

    # 1. At ingest, `huge.json` exceeds max_file_bytes=800 -> quarantined without storing blob.
    r_ing = cli(
        "run",
        "--step",
        "ingest",
        "--workspace",
        str(ws_root),
        "--source",
        str(src),
        "--json",
        env={"COMMISSURE_MAX_FILE_BYTES": "800"},
    )
    assert r_ing.exit_code == 0, r_ing.output
    ing_rep = json.loads(r_ing.stdout)
    assert (ing_rep["added"], ing_rep["quarantined"]) == (2, 1)
    ing_q = _query(
        ws_root,
        "SELECT outcome, reason_code, diagnostics FROM outcomes WHERE stage = 'ingest'",
    )
    assert len(ing_q) == 1
    assert ing_q[0]["reason_code"] == "file_too_large"
    assert json.loads(ing_q[0]["diagnostics"])[0]["detail"]["limit"] == 800

    # 2. At parse, `tall.csv` (11 non-empty rows) exceeds max_table_rows=5 -> quarantined.
    r_p1 = cli(
        "run",
        "--step",
        "parse",
        "--workspace",
        str(ws_root),
        "--json",
        env={"COMMISSURE_MAX_TABLE_ROWS": "5"},
    )
    assert r_p1.exit_code == 0, r_p1.output
    p1 = json.loads(r_p1.stdout)
    assert (p1["valid"], p1["quarantined"]) == (1, 1)

    # 3. Same limit again -> clean cache noop (quarantined item is not retried unnecessarily).
    r_p2 = cli(
        "run",
        "--step",
        "parse",
        "--workspace",
        str(ws_root),
        "--json",
        env={"COMMISSURE_MAX_TABLE_ROWS": "5"},
    )
    assert r_p2.exit_code == 0
    p2 = json.loads(r_p2.stdout)
    assert (p2["status"], p2["unchanged"], p2["attempted"]) == ("noop", 2, 0)

    # 4. Raise max_table_rows=20 -> `small.csv` stays cached (`unchanged == 1`), `tall.csv` retries!
    r_p3 = cli(
        "run",
        "--step",
        "parse",
        "--workspace",
        str(ws_root),
        "--json",
        env={"COMMISSURE_MAX_TABLE_ROWS": "20"},
    )
    assert r_p3.exit_code == 0, r_p3.output
    p3 = json.loads(r_p3.stdout)
    assert (p3["unchanged"], p3["attempted"], p3["valid"]) == (1, 1, 1)


def test_pdf_archive_and_extracted_bytes_limits(cli, ws_root, tmp_path):
    """PDF page cap, OOXML zip-bomb cap, and extracted payload cap quarantine cleanly."""
    src = tmp_path / "src"
    src.mkdir()
    _write_text_pdf(src / "ok.pdf", page_count=2)
    _write_text_pdf(src / "many_pages.pdf", page_count=6)

    wb = Workbook()
    wb.active["A1"] = "hello"
    bomb_xlsx = src / "bomb.xlsx"
    wb.save(bomb_xlsx)
    # Append a highly compressible 300 KB member into the OOXML zip (~400 bytes compressed).
    with zipfile.ZipFile(bomb_xlsx, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/media/padding.bin", b"\0" * 300_000)

    (src / "verbose.json").write_text(json.dumps({"items": list(range(300))}), encoding="utf-8")

    env = {
        "COMMISSURE_MAX_PDF_PAGES": "3",
        "COMMISSURE_MAX_ARCHIVE_BYTES": "150000",
        "COMMISSURE_MAX_EXTRACTED_BYTES": "500",
    }

    p_probe = cli("probe", str(src), "--json", env=env)
    assert p_probe.exit_code == 0, p_probe.output
    probed = {f["locator"]: f for f in json.loads(p_probe.stdout)["files"]}
    assert probed["ok.pdf"]["status"] == "inspected"
    assert probed["many_pages.pdf"]["status"] == "error"
    assert "too_many_pages" in probed["many_pages.pdf"]["error"]
    assert probed["bomb.xlsx"]["status"] == "error"
    assert "archive_too_large" in probed["bomb.xlsx"]["error"]

    assert cli.ingest(ws_root, src)[0] == 0
    r_parse = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json", env=env)
    assert r_parse.exit_code == 0, r_parse.output
    rep = json.loads(r_parse.stdout)
    assert (rep["valid"], rep["quarantined"]) == (1, 3)

    got = {
        r["locator"]: (r["outcome"], r["reason_code"], r["extracted_text_id"])
        for r in _query(
            ws_root,
            "SELECT i.locator, o.outcome, o.reason_code, o.extracted_text_id"
            " FROM outcomes o JOIN item_versions v ON v.id = o.item_version_id"
            " JOIN items i ON i.id = v.item_id",
        )
    }
    assert got == {
        "ok.pdf": ("valid", None, got["ok.pdf"][2]),
        "many_pages.pdf": ("quarantined", "too_many_pages", None),
        "bomb.xlsx": ("quarantined", "archive_too_large", None),
        "verbose.json": ("quarantined", "extracted_output_too_large", None),
    }
    assert got["ok.pdf"][2] is not None


def test_recipe_limits_precedence_and_invalid_env_rejected(cli, tmp_path, monkeypatch):
    """Recipe LIMITS overrides defaults; env overrides recipe; invalid env fails at startup."""
    sol = tmp_path / "sol"
    shutil.copytree(DEMO, sol / "recipe", ignore=shutil.ignore_patterns("__pycache__"))
    recipe_py = sol / "recipe" / "recipe.py"
    recipe_py.write_text(
        "from commissure.api import Limits\n"
        "LIMITS = Limits(max_source_files=12345)\n" + recipe_py.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    src = sol / "intake"
    src.mkdir()
    (src / "a.json").write_text('{"name": "alpha", "amount": 4}', encoding="utf-8")
    monkeypatch.chdir(sol)

    # Empty or non-positive env var fails fast with exit code 2.
    bad_empty = cli(
        "run", "--step", "ingest", "--source", str(src), env={"COMMISSURE_MAX_PDF_PAGES": ""}
    )
    assert bad_empty.exit_code == 2
    bad_zero = cli(
        "run", "--step", "ingest", "--source", str(src), env={"COMMISSURE_MAX_PDF_PAGES": "0"}
    )
    assert bad_zero.exit_code == 2

    r_ing = cli("run", "--step", "ingest", "--source", str(src), "--json")
    assert r_ing.exit_code == 0, r_ing.output
    ws = open_workspace(sol / "workspace")
    try:
        cfg = json.loads(
            ws.conn.execute(
                "SELECT config FROM runs WHERE id = ?", (json.loads(r_ing.stdout)["run_id"],)
            ).fetchone()[0]
        )
    finally:
        ws.close()
    assert cfg["limits"]["max_source_files"] == {"value": 12345, "from": "recipe"}
    assert cfg["limits"]["max_pdf_pages"]["from"] == "default"

    r_parse = cli(
        "run",
        "--step",
        "parse",
        "--json",
        env={"COMMISSURE_MAX_SOURCE_FILES": "99999"},
    )
    assert r_parse.exit_code == 0, r_parse.output
    ws = open_workspace(sol / "workspace")
    try:
        pcfg = json.loads(
            ws.conn.execute(
                "SELECT config FROM runs WHERE id = ?", (json.loads(r_parse.stdout)["run_id"],)
            ).fetchone()[0]
        )
    finally:
        ws.close()
    assert pcfg["limits"]["max_source_files"] == {"value": 99999, "from": "env"}


def test_serve_fast_status_poll_recipe_cache_and_clean_shutdown(tmp_path, monkeypatch):
    """Busy `/_status` skips `build_view` & `load_recipe`, and `shutdown()` cancels cleanly."""
    coord_key = "_test_serve_resilience_coord"
    entered = threading.Event()
    release = threading.Event()
    sys.modules[coord_key] = (entered, release)
    try:
        sol = tmp_path / "sol"
        recipe_dir = sol / "recipe"
        recipe_dir.mkdir(parents=True)
        (recipe_dir / "recipe.py").write_text(
            f"""import sys, time
from pydantic import BaseModel, ConfigDict
from commissure.api import Interface, Stage

class ItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locator: str

def extract_fn(ctx, item):
    entered, release = sys.modules[{coord_key!r}]
    entered.set()
    # Wait cooperatively so cancellation or release unblocks promptly.
    deadline = time.monotonic() + 30.0
    while not release.is_set() and time.monotonic() < deadline:
        if ctx.cancel_event.is_set():
            break
        time.sleep(0.02)
    return ItemOut(locator=item.locator)

def render_ui(view):
    return "<section>v1</section>"

STAGES = [Stage("extract", extract_fn, schema=ItemOut, text="default")]
INTERFACE = Interface(title="Resilience UI", render=render_ui)
""",
            encoding="utf-8",
        )
        src = sol / "intake"
        src.mkdir()
        for idx in range(4):
            (src / f"item_{idx}.json").write_text(json.dumps({"i": idx}), encoding="utf-8")

        ws_path = sol / "workspace"
        ui_app = InterfaceApp(ws_path, recipe_dir, htmx_wait_seconds=0.02)
        # Seed ingest + parse via ActionContext so extract has runnable items.
        from commissure.serve import ActionContext

        act = ActionContext(ws_path, ui_app._load_current_recipe())
        act.ingest(src)
        act.run_step("parse")

        build_view_calls = 0
        orig_build_view = serve_mod.build_view

        def spy_build_view(*args, **kwargs):
            nonlocal build_view_calls
            build_view_calls += 1
            return orig_build_view(*args, **kwargs)

        load_recipe_calls = 0
        orig_load_recipe = serve_mod.load_recipe

        def spy_load_recipe(*args, **kwargs):
            nonlocal load_recipe_calls
            load_recipe_calls += 1
            return orig_load_recipe(*args, **kwargs)

        monkeypatch.setattr(serve_mod, "build_view", spy_build_view)
        monkeypatch.setattr(serve_mod, "load_recipe", spy_load_recipe)

        # Trigger `extract` via HTMX POST.
        post_resp = ui_app.handle(
            "POST",
            "/_pipeline/run",
            {"content-type": "application/x-www-form-urlencoded", "hx-request": "true"},
            f"csrf_token={ui_app.csrf_token}&step=extract".encode(),
        )
        assert post_resp.status == 200
        assert entered.wait(timeout=5.0)

        before_views = build_view_calls
        before_loads = load_recipe_calls

        # Poll `/_status` 3 times while busy -> zero `build_view` calls, zero `load_recipe` calls!
        for _ in range(3):
            poll_resp = ui_app.handle("GET", "/_status", {})
            assert poll_resp.status == 200
            assert b"workspace-busy-bar" in poll_resp.body
            assert b"Running: extract" in poll_resp.body
        assert build_view_calls == before_views
        assert load_recipe_calls == before_loads

        # Shut down `ui_app` while the run is still in progress (simulating Ctrl-C in `serve`).
        t0 = time.monotonic()
        ui_app.shutdown(timeout=5.0)
        assert time.monotonic() - t0 < 3.0

        # Run row in state.db must NOT be left stuck at `status='running'`.
        extract_runs = _query(
            ws_path, "SELECT status, error FROM runs WHERE step LIKE '%extract' ORDER BY id DESC"
        )
        assert extract_runs[0]["status"] == "failed"

        # Editing `recipe.py` invalidates the fingerprint cache on the very next request.
        rp = recipe_dir / "recipe.py"
        rp.write_text(
            rp.read_text(encoding="utf-8").replace(
                "<section>v1</section>", "<section>v2</section>"
            ),
            encoding="utf-8",
        )
        reloaded = ui_app.handle("GET", "/", {})
        assert reloaded.status == 200
        assert b"<section>v2</section>" in reloaded.body
        assert load_recipe_calls == before_loads + 1
    finally:
        release.set()
        sys.modules.pop(coord_key, None)


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_parse_process_pool_enforces_hard_timeout_and_instant_cancel(tmp_path, mode):
    """Child-process parse pool preemptively kills a wedged worker on timeout or cancel."""
    from commissure.limits import Limits
    from commissure.parse.pool import ParseProcessPool

    slow_blob = tmp_path / "slow.json"
    slow_blob.write_text('{"__test_sleep__": 30}', encoding="utf-8")
    fast_blob = tmp_path / "fast.json"
    fast_blob.write_text('{"ok": 1}', encoding="utf-8")

    limits = Limits(max_parse_seconds=0.4)
    pool = ParseProcessPool(max_workers=1, limits=limits)
    try:
        if mode == "timeout":
            t0 = time.monotonic()
            env = pool.parse_item("json", slow_blob, _test_sleep_s=10.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 3.0
            assert env.outcome == "quarantined"
            assert env.reason_code == "parse_timeout"
            assert env.diagnostics[0].detail["limit"] == 0.4
            # Pool automatically recovers by spawning a fresh worker for the next item. A fresh
            # worker's imports count against the item deadline and can exceed 0.4s, so the wide
            # limit keeps this about recovery rather than how quickly `spawn` starts here.
            pool.limits = Limits(max_parse_seconds=30.0)
            env_fast = pool.parse_item("json", fast_blob)
            assert env_fast.outcome == "valid"
        else:
            cancel_ev = threading.Event()
            timer = threading.Timer(0.25, cancel_ev.set)
            timer.start()
            t0 = time.monotonic()
            with pytest.raises(Exception, match="cancel|interrupt"):
                pool.parse_item("json", slow_blob, cancel_event=cancel_ev, _test_sleep_s=10.0)
            assert time.monotonic() - t0 < 3.0
            timer.cancel()
    finally:
        pool.close()
