"""Golden outputs for `run --step parse`, reviewed as diffs.

Both corpora are ingested and parsed through the CLI, with the default parsers and
again with `--parser pdfium`. For each item and parser, tests/golden/parse/<corpus>/
holds `<parser>/<locator>.txt` (the text) and `<parser>/<locator>.json` (text hash,
structure, outcome, diagnostics), plus `outcomes.json` for every item including those
with no text. Parser versions are left out: they change with every code edit.

To accept a change: COMMISSURE_UPDATE_GOLDEN=1 uv run pytest tests/test_parse_golden.py
then review `git diff tests/golden` before committing.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden" / "parse"
CORPORA = ("probe_corpus", "real_samples")
UPDATE = os.environ.get("COMMISSURE_UPDATE_GOLDEN") == "1"


def _render(ws_root) -> dict[Path, str]:
    from commissure.workspace import open_workspace

    ws = open_workspace(ws_root)
    files: dict[Path, str] = {}
    outcomes: dict[str, dict] = {}
    rows = ws.conn.execute(
        "SELECT i.locator, o.stage, o.outcome, o.reason_code, o.diagnostics,"
        " e.text, e.text_hash, e.structure"
        " FROM outcomes o JOIN item_versions v ON v.id = o.item_version_id"
        " JOIN items i ON i.id = v.item_id"
        " LEFT JOIN extracted_text e ON e.id = o.extracted_text_id"
        " ORDER BY i.locator, o.stage"
    ).fetchall()
    for r in rows:
        tool = r["stage"].removeprefix("parse").removeprefix(".") or "(none)"
        outcomes.setdefault(r["locator"], {})[tool] = {
            "outcome": r["outcome"],
            "reason_code": r["reason_code"],
        }
        if r["text"] is None:
            continue
        base = Path(tool) / r["locator"]
        files[base.with_name(base.name + ".txt")] = r["text"] + "\n"
        record = {
            "text_hash": r["text_hash"],
            "outcome": r["outcome"],
            "reason_code": r["reason_code"],
            "diagnostics": json.loads(r["diagnostics"]),
            "structure": json.loads(r["structure"]),
        }
        files[base.with_name(base.name + ".json")] = (
            json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        )
    files[Path("outcomes.json")] = json.dumps(outcomes, indent=2, ensure_ascii=False) + "\n"
    ws.close()
    return files


@pytest.mark.parametrize("corpus", CORPORA)
def test_parse_matches_golden(cli, tmp_path, corpus):
    source = tmp_path / corpus
    shutil.copytree(FIXTURES / corpus, source)
    ws_root = tmp_path / "ws"
    assert cli.ingest(ws_root, source)[0] == 0
    for extra in ([], ["--parser", "pdfium"]):
        result = cli("run", "--step", "parse", "--workspace", str(ws_root), "--json", *extra)
        assert result.exit_code == 0, result.output

    produced = _render(ws_root)
    golden_dir = GOLDEN / corpus
    if UPDATE:
        shutil.rmtree(golden_dir, ignore_errors=True)
        for rel, content in produced.items():
            (golden_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (golden_dir / rel).write_text(content, encoding="utf-8")
    on_disk = {p.relative_to(golden_dir) for p in golden_dir.rglob("*") if p.is_file()}
    assert on_disk == set(produced), "golden files out of date; see module docstring"
    for rel, content in sorted(produced.items()):
        assert (golden_dir / rel).read_text(encoding="utf-8") == content, rel
