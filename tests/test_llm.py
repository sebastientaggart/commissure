"""Tests for the LLM seam (`commissure/llm.py`), ledger, cache, and startup gates."""

from __future__ import annotations

import builtins
import datetime
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from commissure.api import (
    ExtractionFailed,
    InputTooLarge,
    LLMConfigError,
    LLMTransportError,
    MissingAPIKeyError,
    Part,
    RemoteModelRefused,
    RunContext,
)
from commissure.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE
from commissure.llm import (
    LlmSeam,
    TransportRequest,
    TransportResponse,
    coerce_and_validate,
    parse_model_spec,
    use_transport,
    validate_stage_llm,
)
from commissure.recipes import load_recipe, recipe_plan, register_stages
from commissure.runner import run_plan
from commissure.runs import start_run


class SampleComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stance: str
    quote: str
    score: int = 1
    tags: list[str] = Field(default_factory=list)
    note: str | None = None


class StrictOptionalNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stance: str
    note: str | None  # nullable optional without `= None` default


class StrictScalars(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flag: bool = True
    count: int = 1
    ratio: float = 1.5
    day: datetime.date | None = None


class CannedTransport:
    """Injectable test transport returning queued responses or raising queued exceptions."""

    def __init__(
        self,
        responses: Sequence[TransportResponse | BaseException],
        *,
        delay_s: float = 0.0,
    ) -> None:
        self._queue = list(responses)
        self._lock = threading.Lock()
        self._delay_s = delay_s
        self.requests: list[TransportRequest] = []

    @property
    def hits(self) -> int:
        with self._lock:
            return len(self.requests)

    def request(self, req: TransportRequest) -> TransportResponse:
        if self._delay_s > 0:
            time.sleep(self._delay_s)
        with self._lock:
            self.requests.append(req)
            if not self._queue:
                raise AssertionError("CannedTransport queue exhausted")
            item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_seam(
    ws,
    *,
    params: dict[str, Any] | None = None,
    transport: CannedTransport | None = None,
    no_cache: bool = False,
) -> LlmSeam:
    effective = {"model": "ollama:qwen2.5:7b", "context_tokens": 32_768, **(params or {})}
    run_id = start_run(
        ws.conn,
        "demo.extract",
        trigger="test",
        config={},
        stage_fp="fp1",
        recipe_fp="rfp1",
        params=json.dumps(effective, sort_keys=True),
    )
    return LlmSeam(
        conn=ws.conn,
        run_id=run_id,
        stage="demo.extract",
        stage_params=effective,
        no_cache=no_cache,
        transport=transport,
        subject_key_getter=lambda: "item:1",
    )


def _write_llm_recipe(
    root: Path,
    *,
    model: str | None = "ollama:qwen2.5:7b",
    allow_remote: bool = False,
    extra_params: dict[str, Any] | None = None,
) -> Path:
    recipe_dir = root / "llm_recipe"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    params_dict: dict[str, Any] = {}
    if model is not None:
        params_dict["model"] = model
        params_dict["context_tokens"] = 32_768
    if allow_remote:
        params_dict["allow_remote"] = True
    if extra_params:
        params_dict.update(extra_params)

    (recipe_dir / "recipe.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import (\n"
        "    Diagnostic, ExtractionFailed, InputTooLarge, Part, RecordEnvelope, Stage\n"
        ")\n\n"
        "class Extracted(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    stance: str\n"
        "    quote: str\n"
        "    score: int = 1\n\n"
        "def extract_item(ctx, item):\n"
        "    if ctx.llm is None:\n"
        "        return Extracted(stance='no_llm', quote=item.locator, score=0)\n"
        "    try:\n"
        "        res = ctx.llm.extract(\n"
        "            Extracted,\n"
        "            system='Extract stance and verbatim quote.',\n"
        "            parts=[Part('comment', item.text or item.locator)],\n"
        "        )\n"
        "        return res.payload\n"
        "    except ExtractionFailed as exc:\n"
        "        return RecordEnvelope.quarantined(\n"
        "            'model_output_invalid',\n"
        "            Diagnostic(code='model_output_invalid', message=str(exc)),\n"
        "        )\n"
        "    except InputTooLarge as exc:\n"
        "        return RecordEnvelope.quarantined(\n"
        "            'input_too_large',\n"
        "            Diagnostic(code='input_too_large', message=str(exc)),\n"
        "        )\n\n"
        f"STAGES = [\n"
        f"    Stage(\n"
        f"        'extract',\n"
        f"        extract_item,\n"
        f"        schema=Extracted,\n"
        f"        text='default',\n"
        f"        params={params_dict!r},\n"
        f"    ),\n"
        f"]\n",
        encoding="utf-8",
    )
    return recipe_dir


def _cli_run(cli, *args: str) -> tuple[int, dict[str, Any]]:
    res = cli("run", *args, "--json")
    payload = json.loads(res.stdout) if res.stdout.strip() else {}
    return res.exit_code, payload


def _ingest_and_parse(cli, ws_root: Path, tmp_path: Path, count: int = 3) -> Path:
    src = tmp_path / "corpus"
    src.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        (src / f"c{i}.json").write_text(
            json.dumps({"comment": f"Comment {i}: We support section {i}."}),
            encoding="utf-8",
        )
    code, _ = cli.ingest(ws_root, src)
    assert code == EXIT_OK
    code, _ = _cli_run(cli, "--step", "parse", "--workspace", str(ws_root))
    assert code == EXIT_OK
    return src


# --- 1. Lazy SDK import & ctx.llm=None when no model declared ---------------------


def test_import_commissure_without_pydantic_ai_in_sys_modules(ws, tmp_path, cli, monkeypatch):
    """Importing commissure and running a stage with no model never imports pydantic_ai."""
    code = (
        "import builtins, sys\n"
        "real_import = builtins.__import__\n"
        "def guard(name, *args, **kwargs):\n"
        "    if name == 'pydantic_ai' or name.startswith('pydantic_ai.'):\n"
        "        raise ModuleNotFoundError('pydantic_ai blocked by test')\n"
        "    return real_import(name, *args, **kwargs)\n"
        "builtins.__import__ = guard\n"
        "import commissure, commissure.api, commissure.llm, commissure.runner\n"
        "assert 'pydantic_ai' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    _ingest_and_parse(cli, ws.root, tmp_path, count=1)
    recipe_dir = _write_llm_recipe(tmp_path, model=None)
    loaded = load_recipe(recipe_dir)
    register_stages(ws.conn, loaded)
    plan = recipe_plan(ws, loaded, "extract")

    real_import = builtins.__import__

    def block_sdk(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".", 1)[0] in {"pydantic_ai", "openai", "anthropic"}:
            raise ModuleNotFoundError(f"{name} blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_sdk)
    res = run_plan(ws, plan, trigger="test", config={})
    assert res.status == "succeeded"
    assert res.valid == 1
    row = ws.conn.execute("SELECT payload FROM current_records").fetchone()
    assert json.loads(row["payload"])["stance"] == "no_llm"


# --- 2. Cache hit, ledger rows, --no-cache, complete(), and views -----------------


def test_second_identical_call_is_served_from_cache_and_no_cache_bypasses(ws):
    transport = CannedTransport(
        [
            TransportResponse(
                raw_text='{"stance": "support", "quote": "we agree", "score": 5}',
                input_tokens=40,
                output_tokens=15,
                cost_usd=0.0012,
            ),
            TransportResponse(
                raw_text='{"stance": "support", "quote": "we agree refreshed", "score": 5}',
                input_tokens=40,
                output_tokens=16,
                cost_usd=0.0015,
            ),
            TransportResponse(raw_text="Summary text.", input_tokens=12, output_tokens=4),
        ]
    )
    seam = _make_seam(ws, transport=transport)

    first = seam.extract(
        SampleComment,
        system="Extract stance.",
        parts=[Part("comment", "We agree with the rule.")],
    )
    assert first.cached is False
    assert first.payload.stance == "support"
    assert first.payload.quote == "we agree"
    assert first.repairs == []
    assert transport.hits == 1

    # Second identical call: served from llm_cache without hitting the transport.
    second = seam.extract(
        SampleComment,
        system="Extract stance.",
        parts=[("comment", "We agree with the rule.")],
    )
    assert second.cached is True
    assert second.payload == first.payload
    assert second.call_id != first.call_id
    assert transport.hits == 1

    calls = ws.conn.execute(
        "SELECT id, cached, status, input_tokens, output_tokens, cost_usd, cache_key, "
        "request_hash, prompt_hash FROM llm_calls ORDER BY id"
    ).fetchall()
    assert len(calls) == 2
    assert calls[0]["cached"] == 0 and calls[0]["status"] == "ok"
    assert calls[1]["cached"] == 1 and calls[1]["status"] == "ok"
    assert calls[0]["cache_key"] == calls[1]["cache_key"]
    assert calls[0]["prompt_hash"] == calls[1]["prompt_hash"]

    by_run = ws.conn.execute(
        "SELECT * FROM llm_calls_by_run WHERE run_id = ?", (seam.run_id,)
    ).fetchone()
    assert by_run["calls"] == 2
    assert by_run["ok_calls"] == 2
    assert by_run["cached_calls"] == 1
    assert by_run["cache_hit_rate"] == pytest.approx(0.5)
    assert by_run["invalid_rate"] == pytest.approx(0.0)
    assert by_run["input_tokens"] == 80
    assert by_run["output_tokens"] == 30

    by_model = ws.conn.execute(
        "SELECT * FROM llm_calls_by_model WHERE provider = 'ollama' AND model = 'qwen2.5:7b'"
    ).fetchone()
    assert by_model["calls"] == 2
    assert by_model["cached_calls"] == 1

    # With no_cache=True (--no-cache), reads are bypassed, transport is hit, and cache updates.
    seam_no_cache = _make_seam(ws, transport=transport, no_cache=True)
    third = seam_no_cache.extract(
        SampleComment,
        system="Extract stance.",
        parts=[Part("comment", "We agree with the rule.")],
    )
    assert third.cached is False
    assert third.payload.quote == "we agree refreshed"
    assert transport.hits == 2

    fourth = seam.extract(
        SampleComment,
        system="Extract stance.",
        parts=[Part("comment", "We agree with the rule.")],
    )
    assert fourth.cached is True
    assert fourth.payload.quote == "we agree refreshed"
    assert transport.hits == 2

    # Free-text complete() also caches and records ledger rows:
    c1 = seam.complete(system="Summarize.", parts=[Part("doc", "Paragraph one.")])
    c2 = seam.complete(system="Summarize.", parts=[Part("doc", "Paragraph one.")])
    assert c1.text == "Summary text." and c1.cached is False
    assert c2.text == "Summary text." and c2.cached is True
    assert transport.hits == 3


# --- 3. Egress gate (startup + per-call base_url) and API key checks --------------


def test_remote_model_refused_at_startup_and_on_call_base_url_override(
    ws, tmp_path, cli, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa-test-key")

    for remote in (
        {"model": "openrouter:anthropic/claude-sonnet-4"},
        {"model": "anthropic:claude-sonnet-4-5"},
        {"model": "openai:gpt-4o-mini"},
        {"model": "llamacpp:qwen2.5", "base_url": "http://example.com:8080/v1"},
        {"model": "llamacpp:qwen2.5", "base_url": "http://10.0.0.5:8080/v1"},
        {"model": "ollama:qwen2.5:7b", "base_url": "http://10.0.0.5:11434"},
    ):
        with pytest.raises(RemoteModelRefused, match="allow_remote"):
            validate_stage_llm({**remote, "context_tokens": 32_768})

    for local in (
        {"model": "ollama:qwen2.5:7b"},
        {"model": "llamacpp:qwen2.5-7b"},
        {"model": "llamacpp:qwen2.5", "base_url": "http://localhost:8080/v1"},
        {"model": "llamacpp:qwen2.5", "base_url": "http://127.0.0.1:8080/v1"},
        {"model": "llamacpp:qwen2.5", "base_url": "http://[::1]:8080/v1"},
        {"model": "openai:local-model", "base_url": "http://localhost:1234"},
    ):
        target = validate_stage_llm({**local, "context_tokens": 32_768})
        assert target is not None and target.is_local is True

    with pytest.raises(LLMConfigError):
        parse_model_spec("invalid-no-colon")

    # Per-call params["base_url"] moving a local stage to a remote host is refused
    # with zero transport hits unless allow_remote=True.
    transport = CannedTransport(
        [TransportResponse(raw_text='{"stance": "support", "quote": "x", "score": 1}')]
    )
    seam = _make_seam(ws, params={"model": "ollama:qwen2.5:7b"}, transport=transport)
    with pytest.raises(RemoteModelRefused, match="allow_remote"):
        seam.extract(
            SampleComment,
            system="Extract.",
            parts=[Part("comment", "hi")],
            params={"base_url": "https://remote.example.com/v1"},
        )
    with pytest.raises(RemoteModelRefused, match="allow_remote"):
        seam.complete(
            system="Summarize.",
            parts=[Part("comment", "hi")],
            params={"base_url": "https://remote.example.com/v1"},
        )
    assert transport.hits == 0

    # CLI refuses remote model at startup with EXIT_USAGE (2) and records no run row.
    _ingest_and_parse(cli, ws.root, tmp_path, count=1)
    recipe_dir = _write_llm_recipe(
        tmp_path, model="openrouter:anthropic/claude-sonnet-4", allow_remote=False
    )
    runs_before = ws.conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    with use_transport(transport):
        res = cli(
            "run",
            "--step",
            "extract",
            "--recipe",
            str(recipe_dir),
            "--workspace",
            str(ws.root),
        )
    assert res.exit_code == EXIT_USAGE
    assert "allow_remote" in res.output
    assert transport.hits == 0
    assert ws.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == runs_before


def test_missing_api_key_is_startup_error(ws, tmp_path, cli, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    for spec, expected_var in (
        ("openrouter:anthropic/claude-sonnet-4", "OPENROUTER_API_KEY"),
        ("anthropic:claude-sonnet-4-5", "ANTHROPIC_API_KEY"),
        ("openai:gpt-4o-mini", "OPENAI_API_KEY"),
    ):
        with pytest.raises(MissingAPIKeyError, match=expected_var):
            validate_stage_llm({"model": spec, "allow_remote": True})

    # Even if a .env file sits in cwd with the key, core never reads .env.
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    recipe_dir = _write_llm_recipe(
        tmp_path, model="openrouter:anthropic/claude-sonnet-4", allow_remote=True
    )
    res = cli(
        "run",
        "--step",
        "extract",
        "--recipe",
        str(recipe_dir),
        "--workspace",
        str(ws.root),
    )
    assert res.exit_code == EXIT_USAGE
    assert "OPENROUTER_API_KEY" in res.output


def test_context_tokens_is_required_at_startup(ws, tmp_path, cli):
    # Core keeps no model table: a guessed context window is a silent default.
    with pytest.raises(LLMConfigError, match="context_tokens"):
        validate_stage_llm({"model": "ollama:qwen2.5:7b"})
    for bad in (0, -1, True, "32768"):
        with pytest.raises(LLMConfigError, match="context_tokens"):
            validate_stage_llm({"model": "ollama:qwen2.5:7b", "context_tokens": bad})

    _ingest_and_parse(cli, ws.root, tmp_path, count=1)
    recipe_dir = _write_llm_recipe(tmp_path, extra_params={"context_tokens": None})
    runs_before = ws.conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    res = cli("run", "--step", "extract", "--recipe", str(recipe_dir), "--workspace", str(ws.root))
    assert res.exit_code == EXIT_USAGE
    assert "context_tokens" in res.output
    assert ws.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == runs_before


def test_base_url_gets_v1_for_openai_compatible_but_not_anthropic():
    # The Anthropic SDK appends `/v1/messages` itself; `/v1` here would double it.
    anth = parse_model_spec("anthropic:claude-sonnet-4-5", {"base_url": "http://localhost:4000/"})
    assert anth.base_url == "http://localhost:4000"
    local = parse_model_spec("llamacpp:m", {"base_url": "http://localhost:9931"})
    assert local.base_url == "http://localhost:9931/v1"
    assert parse_model_spec("ollama:m").base_url == "http://localhost:11434/v1"
    assert parse_model_spec("anthropic:m").base_url is None


# --- 4. Coercion repairs & strict validation boundary -----------------------------


def test_coercion_fenced_json_with_trailing_prose_and_all_repairs(ws):
    raw = (
        "Here is the extracted record:\n"
        "```json\n"
        "{\n"
        "  'stance': 'oppose',\n"
        "  'quote': \"Section 4 imposes excessive costs\",\n"
        "  'score': '42',\n"
        "  'tags': null,\n"
        "}\n"
        "```\n"
        "Hope this helps with your analysis!"
    )
    transport = CannedTransport([TransportResponse(raw_text=raw)])
    seam = _make_seam(ws, transport=transport)
    ext = seam.extract(
        SampleComment,
        system="Extract comment fields.",
        parts=[Part("comment", "Section 4 imposes excessive costs.")],
    )
    assert ext.payload.stance == "oppose"
    assert ext.payload.quote == "Section 4 imposes excessive costs"
    assert ext.payload.score == 42
    assert ext.payload.tags == []
    assert ext.raw == raw
    assert set(ext.repairs) == {
        "fenced_json",
        "trailing_prose",
        "trailing_commas",
        "single_quotes",
        "string_numbers",
        "null_for_missing_optional",
    }
    row = ws.conn.execute(
        "SELECT repairs_count, status FROM llm_calls WHERE id = ?", (ext.call_id,)
    ).fetchone()
    assert row["repairs_count"] == 6
    assert row["status"] == "ok"

    # Missing nullable optional without `= None` default is repaired with None:
    payload, repairs = coerce_and_validate('{"stance": "support"}', StrictOptionalNote)
    assert payload.stance == "support"
    assert payload.note is None
    assert "null_for_missing_optional" in repairs

    # Unrepaired Pydantic lax coercions (bool "true"/1, float 1.0 for int, ISO date strings
    # for date) must be rejected with status='invalid'. An integer for a float is not lax:
    # JSON has one number type.
    bad_scalars = [
        '{"flag": "true"}',
        '{"flag": 1}',
        '{"count": 1.0}',
        '{"day": "2026-10-25"}',
    ]
    strict_transport = CannedTransport([TransportResponse(raw_text=b) for b in bad_scalars])
    strict_seam = _make_seam(
        ws,
        params={"max_consecutive_failures": 10},
        transport=strict_transport,
    )
    for i, _ in enumerate(bad_scalars):
        with pytest.raises(ExtractionFailed):
            strict_seam.extract(
                StrictScalars,
                system="Strict scalars.",
                parts=[Part("item", f"case {i}")],
            )
    invalid_count = ws.conn.execute(
        "SELECT count(*) FROM llm_calls WHERE run_id = ? AND status = 'invalid'",
        (strict_seam.run_id,),
    ).fetchone()[0]
    assert invalid_count == len(bad_scalars)

    payload, repairs = coerce_and_validate('{"ratio": 1}', StrictScalars)
    assert payload.ratio == 1.0 and repairs == []


def test_unfixable_response_raises_extraction_failed_and_stage_quarantines(ws, tmp_path, cli):
    _ingest_and_parse(cli, ws.root, tmp_path, count=2)
    recipe_dir = _write_llm_recipe(tmp_path, model="ollama:qwen2.5:7b")

    bad_raw = '{"stance": "support", "unexpected_field": 123}'  # missing quote + extra field
    good_raw = '{"stance": "support", "quote": "We support section 2.", "score": 2}'
    transport = CannedTransport(
        [
            TransportResponse(raw_text=bad_raw),
            TransportResponse(raw_text=good_raw),
        ]
    )
    with use_transport(transport):
        code, report = _cli_run(
            cli,
            "--step",
            "extract",
            "--recipe",
            str(recipe_dir),
            "--workspace",
            str(ws.root),
            "--workers",
            "1",
        )
    assert code == EXIT_OK
    assert report["valid"] == 1
    assert report["quarantined"] == 1

    issue = ws.conn.execute(
        "SELECT outcome, reason_code FROM outcome_issues WHERE stage = 'llm_recipe.extract'"
    ).fetchone()
    assert issue["outcome"] == "quarantined"
    assert issue["reason_code"] == "model_output_invalid"

    invalid_call = ws.conn.execute(
        "SELECT status, error, cache_key FROM llm_calls WHERE status = 'invalid'"
    ).fetchone()
    assert invalid_call is not None
    cached_row = ws.conn.execute(
        "SELECT raw_response FROM llm_cache WHERE cache_key = ?", (invalid_call["cache_key"],)
    ).fetchone()
    assert cached_row["raw_response"] == bad_raw


# --- 5. InputTooLarge (no truncation) ---------------------------------------------


def test_input_too_large_refuses_without_truncating_or_calling_transport(ws):
    transport = CannedTransport(
        [TransportResponse(raw_text='{"stance": "support", "quote": "x", "score": 1}')]
    )
    seam = _make_seam(ws, params={"context_tokens": 100, "reserve_tokens": 25}, transport=transport)
    huge_text = "word " * 400
    with pytest.raises(InputTooLarge) as exc_info:
        seam.extract(
            SampleComment,
            system="Extract stance.",
            parts=[Part("comment", huge_text)],
        )
    assert exc_info.value.input_tokens > exc_info.value.limit_tokens
    assert transport.hits == 0

    call = ws.conn.execute("SELECT status, error, input_tokens FROM llm_calls").fetchone()
    assert call["status"] == "refused"
    assert "input_too_large" in call["error"]
    assert call["input_tokens"] == exc_info.value.input_tokens


# --- 6. Transient retries vs non-transient / validation failure no-retry ----------


def test_transient_faults_retry_within_budget_and_non_transient_do_not(ws):
    transport = CannedTransport(
        [
            TimeoutError("request timed out"),
            LLMTransportError("HTTP 429 Too Many Requests", status_code=429, transient=True),
            LLMTransportError("HTTP 503 Service Unavailable", status_code=503, transient=True),
            TransportResponse(raw_text='{"stance": "support", "quote": "ok", "score": 1}'),
        ]
    )
    seam = _make_seam(ws, params={"retry_budget_s": 5.0}, transport=transport)
    ext = seam.extract(
        SampleComment,
        system="Extract.",
        parts=[Part("comment", "ok")],
    )
    assert ext.payload.stance == "support"
    assert transport.hits == 4

    # A non-transient RuntimeError whose message mentions "500" and "connection"
    # must NOT be retried (1 attempt, status='transport_error'):
    non_transient = CannedTransport(
        [
            RuntimeError("Bad request 400: limit 500 words on connection pool"),
            TransportResponse(raw_text='{"stance": "support", "quote": "ok", "score": 1}'),
        ]
    )
    seam_400 = _make_seam(ws, params={"retry_budget_s": 5.0}, transport=non_transient)
    with pytest.raises(LLMTransportError):
        seam_400.extract(
            SampleComment,
            system="Extract.",
            parts=[Part("comment", "non-transient error prompt")],
        )
    assert non_transient.hits == 1
    row_400 = ws.conn.execute(
        "SELECT status FROM llm_calls WHERE run_id = ?", (seam_400.run_id,)
    ).fetchone()
    assert row_400["status"] == "transport_error"

    # Validation failure must NEVER retry the transport:
    bad_transport = CannedTransport(
        [
            TransportResponse(raw_text="completely unparseable garbage"),
            TransportResponse(raw_text='{"stance": "support", "quote": "ok", "score": 1}'),
        ]
    )
    seam2 = _make_seam(ws, params={"retry_budget_s": 5.0}, transport=bad_transport)
    with pytest.raises(ExtractionFailed) as exc_info:
        seam2.extract(
            SampleComment,
            system="Extract.",
            parts=[Part("comment", "different prompt")],
        )
    assert exc_info.value.raw == "completely unparseable garbage"
    assert bad_transport.hits == 1


# --- 7. Stage-level circuit breaker (sequential, threshold=1 resume, and pool) ----


def test_circuit_breaker_checkpoints_tripping_subject_and_handles_resume_and_pool(
    ws, tmp_path, cli
):
    _ingest_and_parse(cli, ws.root, tmp_path, count=5)

    # Case A: sequential (--workers 1) with max_consecutive_failures=3 checkpoints all 3
    # quarantined outcomes before failing the run with model_systematic_failure.
    recipe_dir = _write_llm_recipe(
        tmp_path,
        model="ollama:qwen2.5:7b",
        extra_params={"max_consecutive_failures": 3},
    )
    transport = CannedTransport([TransportResponse(raw_text=f"invalid {i}") for i in range(5)])
    with use_transport(transport):
        code, report = _cli_run(
            cli,
            "--step",
            "extract",
            "--recipe",
            str(recipe_dir),
            "--workspace",
            str(ws.root),
            "--workers",
            "1",
        )
    assert code == EXIT_FAILED
    assert report["status"] == "failed"
    assert "model_systematic_failure" in (report["error"] or "")
    assert transport.hits == 3
    assert report["quarantined"] == 3
    assert report["remaining"] == 2

    # Case B: threshold=1 persists the quarantined outcome and fails the run;
    # a later resume skips that subject (unchanged=1) and finishes the remaining subject.
    ws1_root = tmp_path / "ws_one"
    _ingest_and_parse(cli, ws1_root, tmp_path / "one_src", count=2)
    recipe_one = _write_llm_recipe(
        tmp_path / "one_rec",
        model="ollama:qwen2.5:7b",
        extra_params={"max_consecutive_failures": 1},
    )
    t_one = CannedTransport(
        [
            TransportResponse(raw_text="broken 1"),
            TransportResponse(raw_text='{"stance": "support", "quote": "ok", "score": 1}'),
        ]
    )
    with use_transport(t_one):
        code1, rep1 = _cli_run(
            cli,
            "--step",
            "extract",
            "--recipe",
            str(recipe_one),
            "--workspace",
            str(ws1_root),
            "--workers",
            "1",
        )
        assert code1 == EXIT_FAILED
        assert rep1["quarantined"] == 1
        assert rep1["remaining"] == 1
        assert t_one.hits == 1

        # Resume: first subject is already done in the resumed chain (unchanged=1),
        # so the run advances to the second subject without spinning on the first.
        code2, rep2 = _cli_run(
            cli,
            "--step",
            "extract",
            "--recipe",
            str(recipe_one),
            "--workspace",
            str(ws1_root),
            "--workers",
            "1",
        )
        assert code2 == 0
        assert rep2["resumed_run_id"] == rep1["run_id"]
        assert rep2["unchanged"] == 1
        assert rep2["valid"] == 1
        assert rep2["remaining"] == 0
        assert t_one.hits == 2

    # Case C: workers > 1 writes every finished subject (including the one that tripped
    # the breaker) and does not call the transport for cancelled unstarted subjects.
    ws_pool = tmp_path / "ws_pool"
    _ingest_and_parse(cli, ws_pool, tmp_path / "pool_src", count=5)
    recipe_pool = _write_llm_recipe(
        tmp_path / "pool_rec",
        model="ollama:qwen2.5:7b",
        extra_params={"max_consecutive_failures": 2},
    )
    t_pool = CannedTransport(
        [TransportResponse(raw_text=f"pool invalid {i}") for i in range(5)],
        delay_s=0.05,
    )
    with use_transport(t_pool):
        code_p, rep_p = _cli_run(
            cli,
            "--step",
            "extract",
            "--recipe",
            str(recipe_pool),
            "--workspace",
            str(ws_pool),
            "--workers",
            "2",
        )
    assert code_p == EXIT_FAILED
    assert "model_systematic_failure" in (rep_p["error"] or "")
    assert rep_p["quarantined"] == t_pool.hits
    assert 2 <= t_pool.hits < 5


# --- 8. Secrets never appear anywhere in state.db ---------------------------------


def test_database_never_contains_api_key(ws, monkeypatch):
    secret = "sk-or-v1-SUPER-SECRET-SENTINEL-9988776655"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    transport = CannedTransport(
        [
            TransportResponse(
                raw_text=f'{{"stance": "support", "quote": "echo {secret}", "score": 1}}',
                input_tokens=10,
                output_tokens=8,
                cost_usd=0.0001,
            ),
            RuntimeError(f"upstream failed with bearer {secret} in header"),
        ]
    )
    seam = _make_seam(
        ws,
        params={
            "model": "openrouter:anthropic/claude-sonnet-4",
            "allow_remote": True,
            "retry_budget_s": 0.0,
        },
        transport=transport,
    )
    ext = seam.extract(
        SampleComment,
        system=f"System with {secret}.",
        parts=[Part("comment", f"User text with {secret}")],
    )
    assert ext.payload.stance == "support"

    with pytest.raises(LLMTransportError):
        seam.extract(
            SampleComment,
            system="Extract.",
            parts=[Part("comment", "trigger error containing secret")],
        )

    ws.conn.execute("PRAGMA wal_checkpoint(FULL)")
    for db_file in ws.root.glob("state.db*"):
        if db_file.is_file():
            raw_bytes = db_file.read_bytes()
            assert secret.encode("utf-8") not in raw_bytes, f"secret leaked into {db_file.name}"

    cache_row = ws.conn.execute("SELECT request_json, raw_response FROM llm_cache").fetchone()
    call_row = ws.conn.execute(
        "SELECT request_hash FROM llm_calls WHERE id = ?", (ext.call_id,)
    ).fetchone()
    import hashlib

    assert (
        hashlib.sha256(cache_row["request_json"].encode("utf-8")).hexdigest()
        == (call_row["request_hash"])
    )
    assert "[REDACTED]" in cache_row["request_json"]
    assert "[REDACTED]" in cache_row["raw_response"]


# --- 9. Concurrency and cache stampede deduplication -------------------------------


def test_ctx_map_parallel_calls_deduplicate_cache_key(ws):
    transport = CannedTransport(
        [TransportResponse(raw_text='{"stance": "support", "quote": "shared", "score": 7}')],
        delay_s=0.05,
    )
    seam = _make_seam(
        ws,
        params={"model": "ollama:qwen2.5:7b"},
        transport=transport,
    )
    ctx = RunContext(
        seam.run_id,
        "demo.extract",
        {"model": "ollama:qwen2.5:7b"},
        workers=4,
        llm=seam,
    )

    def worker(_: int) -> int:
        assert ctx.llm is not None
        res = ctx.llm.extract(
            SampleComment,
            system="Extract.",
            parts=[Part("comment", "identical comment across 4 parallel workers")],
        )
        return res.payload.score

    scores = ctx.map(worker, range(4))
    assert scores == [7, 7, 7, 7]
    assert transport.hits == 1
    rows = ws.conn.execute(
        "SELECT sum(cached = 0), sum(cached = 1) FROM llm_calls WHERE run_id = ?",
        (seam.run_id,),
    ).fetchone()
    assert rows[0] == 1 and rows[1] == 3


# --- 10. Real PydanticAI transport & Ollama live smoke test -----------------------


def test_pydantic_ai_transport_against_local_http_endpoint(ws):
    """Exercise the real PydanticAITransport against a local OpenAI-compatible server."""
    import http.server

    captured_payloads: list[dict[str, Any]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw_req = self.rfile.read(length)
            captured_payloads.append(json.loads(raw_req.decode("utf-8")))
            body = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "qwen2.5-local",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"stance": "support", '
                                    '"quote": "from local server", "score": 9}'
                                ),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 25, "completion_tokens": 14, "total_tokens": 39},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        seam = _make_seam(
            ws,
            params={"model": "llamacpp:qwen2.5-local", "base_url": f"http://127.0.0.1:{port}/v1"},
        )
        ext = seam.extract(
            SampleComment,
            system="Extract stance and quote.",
            parts=[Part("comment", "We support this local server test.")],
        )
        assert ext.payload.stance == "support"
        assert ext.payload.quote == "from local server"
        assert ext.payload.score == 9
        assert len(captured_payloads) == 1
        assert captured_payloads[0].get("response_format") == {"type": "json_object"}
        row = ws.conn.execute("SELECT * FROM llm_calls WHERE id = ?", (ext.call_id,)).fetchone()
        assert row["provider"] == "llamacpp"
        assert row["model"] == "qwen2.5-local"
        assert row["input_tokens"] == 25
        assert row["output_tokens"] == 14
        assert row["cost_usd"] is None
        assert row["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()


def _ollama_small_model() -> tuple[str | None, str | None]:
    """Return `(model_name, skip_reason)` by probing `http://localhost:11434/api/tags`."""
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.5) as resp:
            if resp.status != 200:
                return None, f"Ollama at localhost:11434 returned HTTP {resp.status}"
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"Ollama is not reachable at localhost:11434 ({exc})"

    models = [
        m.get("name")
        for m in data.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("name"), str)
    ]
    chat_models = [m for m in models if "embed" not in m.lower()]
    if not chat_models:
        return None, "Ollama is reachable at localhost:11434 but has no chat models installed"
    return chat_models[0], None


def test_ollama_live_smoke_extract(ws):
    model_name, skip_reason = _ollama_small_model()
    if skip_reason is not None or model_name is None:
        pytest.skip(skip_reason or "Ollama not available")

    class TinySmoke(BaseModel):
        model_config = ConfigDict(extra="forbid")

        topic: str

    seam = _make_seam(ws, params={"model": f"ollama:{model_name}"})
    ext = seam.extract(
        TinySmoke,
        system="Extract the single-word topic of the paragraph as JSON {'topic': '...'}.",
        parts=[
            Part(
                "paragraph",
                "Solar panels convert sunlight directly into electricity for homes and grids.",
            )
        ],
    )
    assert isinstance(ext.payload, TinySmoke)
    assert ext.payload.topic.strip() != ""
    assert ext.cached is False

    row = ws.conn.execute("SELECT * FROM llm_calls WHERE id = ?", (ext.call_id,)).fetchone()
    assert row is not None
    assert row["provider"] == "ollama"
    assert row["model"] == model_name
    assert row["status"] == "ok"
    assert row["cached"] == 0
    assert row["input_tokens"] > 0
    assert row["output_tokens"] > 0


# --- 11. Retry budget after first failure, debug context, and local worker default -


def test_retry_budget_measured_after_first_failure_and_pydantic_ai_errors_classified(ws):
    from pydantic_ai.exceptions import (
        ModelConnectionError,
        ModelOverloadedError,
        ModelQuotaExceededError,
        ModelRateLimitError,
        ModelRequestTimeout,
        ModelServerError,
        ModelTimeoutError,
        ModelUnavailableError,
    )

    from commissure.llm import _classify_transport_exception

    for transient_exc in (
        ModelTimeoutError("gpt-oss", "Request timed out."),
        ModelRequestTimeout("gpt-oss", 10.0),
        ModelConnectionError("gpt-oss", "Connection reset."),
        ModelRateLimitError("gpt-oss", "Too many requests."),
        ModelUnavailableError("gpt-oss", "Unavailable."),
        ModelOverloadedError("gpt-oss", "Overloaded."),
        ModelServerError("gpt-oss", "Server error."),
    ):
        classified = _classify_transport_exception(transient_exc)
        assert classified.transient is True, type(transient_exc).__name__

    quota_err = _classify_transport_exception(
        ModelQuotaExceededError("gpt-oss", "Insufficient quota.")
    )
    assert quota_err.transient is False

    peg_err = _classify_transport_exception(
        ModelServerError(
            "gpt-oss",
            "The model produced output that does not match the expected peg-native format",
        )
    )
    assert peg_err.transient is False

    # Attempt 1 sleeps longer (0.12s) than `retry_budget_s` (0.08s) before timing out.
    # Because `retry_budget_s` governs retry time *after* the first failure, attempt 2 must run.
    class SlowFirstTimeoutTransport:
        def __init__(self) -> None:
            self.hits = 0

        def request(self, req: TransportRequest) -> TransportResponse:
            self.hits += 1
            if self.hits == 1:
                time.sleep(0.12)
                raise ModelTimeoutError(req.model, "Request timed out.")
            return TransportResponse(raw_text='{"stance": "support", "quote": "recovered"}')

    slow_transport = SlowFirstTimeoutTransport()
    seam = _make_seam(
        ws,
        params={"timeout_s": 300.0, "retry_budget_s": 0.08},
        transport=slow_transport,  # type: ignore[arg-type]
    )
    ext = seam.extract(
        SampleComment,
        system="Extract.",
        parts=[Part("comment", "retry after slow timeout")],
    )
    assert ext.payload.quote == "recovered"
    assert slow_transport.hits == 2


def test_transport_error_and_empty_thinking_response_carry_debug_context(ws):
    from pydantic_ai.exceptions import ModelTimeoutError

    failing_transport = CannedTransport([ModelTimeoutError("gpt-oss", "Request timed out.")])
    seam = _make_seam(
        ws,
        params={
            "model": "llamacpp:ggml-org/gpt-oss-20b-GGUF:MXFP4",
            "base_url": "http://localhost:9931/v1",
            "timeout_s": 300.0,
            "retry_budget_s": 0.0,
        },
        transport=failing_transport,
    )
    with pytest.raises(LLMTransportError) as exc_info:
        seam.extract(
            SampleComment,
            system="Extract.",
            parts=[Part("comment", "sample body")],
        )
    msg = str(exc_info.value)
    call_row = ws.conn.execute(
        "SELECT id FROM llm_calls WHERE run_id = ? ORDER BY id DESC LIMIT 1",
        (seam.run_id,),
    ).fetchone()
    assert call_row is not None
    assert "subject=item:1" in msg
    assert "llamacpp:ggml-org/gpt-oss-20b-GGUF:MXFP4 @ http://localhost:9931/v1" in msg
    assert "attempts=1" in msg
    assert "timeout_s=300.0" in msg
    assert "est_input_tokens=" in msg
    assert f"call_id={call_row['id']}" in msg
    assert exc_info.value.call_id == call_row["id"]

    # Reasoning model that burned 59,538 output tokens into thinking and emitted empty final text:
    thinking_only = CannedTransport(
        [TransportResponse(raw_text="", input_tokens=400, output_tokens=59538)]
    )
    seam_think = _make_seam(ws, transport=thinking_only)
    with pytest.raises(ExtractionFailed) as think_exc:
        seam_think.extract(
            SampleComment,
            system="Extract.",
            parts=[Part("comment", "reasoning loop trigger")],
        )
    assert "59538" in str(think_exc.value)
    assert "empty" in str(think_exc.value).lower()


def test_local_llm_stage_defaults_to_one_worker_unless_overridden(
    cli, ws_root, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key-123456")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text('{"x": 1}', encoding="utf-8")
    (src / "b.json").write_text('{"x": 2}', encoding="utf-8")
    assert cli.ingest(ws_root, src)[0] == 0
    assert cli("run", "--step", "parse", "--workspace", str(ws_root), "--json").exit_code == 0

    recipe_dir = tmp_path / "worker_recipe"
    recipe_dir.mkdir()
    (recipe_dir / "recipe.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "from commissure.api import Stage\n\n"
        "class Out(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    workers: int\n\n"
        "def see_workers(ctx, item):\n"
        "    return Out(workers=ctx.workers)\n\n"
        "STAGES = [\n"
        "    Stage('local_llm', see_workers, schema=Out,\n"
        "          params={'model': 'llamacpp:local-model',\n"
        "                  'base_url': 'http://localhost:9931/v1', 'context_tokens': 8192}),\n"
        "    Stage('remote_llm', see_workers, schema=Out,\n"
        "          params={'model': 'openrouter:anthropic/claude-sonnet-4',\n"
        "                  'allow_remote': True, 'context_tokens': 8192}),\n"
        "]\n",
        encoding="utf-8",
    )

    # 1. Local LLM without --workers defaults to workers=1.
    r1 = cli(
        "run",
        "--step",
        "local_llm",
        "--workspace",
        str(ws_root),
        "--recipe",
        str(recipe_dir),
        "--json",
    )
    assert r1.exit_code == 0, r1.output
    run1_id = json.loads(r1.stdout)["run_id"]

    # 2. Explicit --workers 3 overrides the local-LLM default.
    r2 = cli(
        "run",
        "--step",
        "local_llm",
        "--workers",
        "3",
        "--fresh",
        "--workspace",
        str(ws_root),
        "--recipe",
        str(recipe_dir),
        "--json",
    )
    assert r2.exit_code == 0, r2.output
    run2_id = json.loads(r2.stdout)["run_id"]

    from commissure.workspace import open_workspace

    opened = open_workspace(ws_root)
    try:
        rec1 = json.loads(
            opened.conn.execute(
                "SELECT r.payload FROM outcomes o JOIN records r ON r.id = o.record_id"
                " WHERE o.run_id = ? LIMIT 1",
                (run1_id,),
            ).fetchone()[0]
        )
        cfg1 = json.loads(
            opened.conn.execute("SELECT config FROM runs WHERE id = ?", (run1_id,)).fetchone()[0]
        )
        rec2 = json.loads(
            opened.conn.execute(
                "SELECT r.payload FROM outcomes o JOIN records r ON r.id = o.record_id"
                " WHERE o.run_id = ? LIMIT 1",
                (run2_id,),
            ).fetchone()[0]
        )
    finally:
        opened.close()

    assert rec1["workers"] == 1
    assert cfg1["workers"] == {"value": 1, "from": "local-llm-default"}
    assert rec2["workers"] == 3


def _dead_local_url() -> str:
    """A localhost base_url with nothing listening (bound, then closed)."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def test_dead_local_server_fails_run_fast_naming_url_with_no_outcomes(cli, ws, tmp_path):
    """Nothing listening: the run fails in seconds (not the 120s retry budget), names the
    URL, and writes no outcome and no model call."""
    base_url = _dead_local_url()
    _ingest_and_parse(cli, ws.root, tmp_path, count=2)
    recipe_dir = _write_llm_recipe(
        tmp_path, model="llamacpp:wanted", extra_params={"base_url": base_url}
    )
    started = time.monotonic()
    code, report = _cli_run(
        cli, "--step", "extract", "--recipe", str(recipe_dir), "--workspace", str(ws.root)
    )
    assert time.monotonic() - started < 15
    assert code == EXIT_FAILED
    assert f"{base_url}/models" in (report.get("error") or "")
    assert "wanted" in report["error"]
    assert (report["valid"], report["warning"], report["quarantined"]) == (0, 0, 0)
    assert (
        ws.conn.execute("SELECT count(*) FROM outcomes WHERE stage LIKE '%extract'").fetchone()[0]
        == 0
    )
    assert ws.conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == 0


def test_run_served_from_cache_needs_no_model_server(cli, ws, tmp_path):
    """A re-run whose calls all hit `llm_cache` succeeds with the server down: the probe
    runs only before a call that misses the cache."""
    base_url = _dead_local_url()
    _ingest_and_parse(cli, ws.root, tmp_path, count=2)
    recipe_dir = _write_llm_recipe(
        tmp_path, model="llamacpp:wanted", extra_params={"base_url": base_url}
    )
    raw = '{"stance": "support", "quote": "x", "score": 1}'
    transport = CannedTransport([TransportResponse(raw_text=raw)] * 2)
    args = ("--step", "extract", "--recipe", str(recipe_dir), "--workspace", str(ws.root))
    with use_transport(transport):
        code, first = _cli_run(cli, *args)
    assert code == EXIT_OK and first["valid"] == 2

    # Change the recipe fingerprint without changing the prompt: every subject re-runs,
    # every call is a cache hit, and nothing listens at base_url.
    recipe_py = recipe_dir / "recipe.py"
    recipe_py.write_text(recipe_py.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    code, second = _cli_run(cli, *args)
    assert code == EXIT_OK, second
    assert second["valid"] == 2
    assert transport.hits == 2


def test_seam_probes_once_and_reuses_the_failure(ws, monkeypatch):
    from commissure.llm import LLMTransportError

    probes: list[str] = []

    def failing_probe(target: Any, **_k: Any) -> None:
        probes.append(target.base_url)
        raise LLMTransportError(f"local model server at {target.base_url}/models is not reachable")

    monkeypatch.setattr("commissure.llm.probe_local_model", failing_probe)
    seam = _make_seam(ws, params={"model": "llamacpp:m", "base_url": "http://127.0.0.1:1/v1"})
    for _ in range(2):
        with pytest.raises(LLMTransportError, match="127.0.0.1:1/v1/models"):
            seam.extract(SampleComment, system="Extract.", parts=[Part("comment", "hi")])
    assert probes == ["http://127.0.0.1:1/v1"]


class _ModelsResponse:
    def __init__(self, payload: Any) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _ModelsResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _probe(payload: Any = None, *, error: BaseException | None = None, model: str = "wanted"):
    from commissure.llm import probe_local_model

    def opener(req: Any, timeout: float = 3.0) -> Any:
        if error is not None:
            raise error
        return _ModelsResponse(payload)

    probe_local_model(
        parse_model_spec(f"llamacpp:{model}", {"base_url": "http://127.0.0.1:8080/v1"}),
        urlopen=opener,
    )


def test_local_model_probe_fails_only_when_nothing_answers_or_model_is_unlisted() -> None:
    import io

    from commissure.llm import LLMTransportError

    with pytest.raises(LLMTransportError, match="127.0.0.1:8080/v1/models is not reachable"):
        _probe(error=urllib.error.URLError(ConnectionRefusedError("Connection refused")))

    # Several models listed, none named `wanted` by id or alias.
    many = {"data": [{"id": "a"}, {"id": "b", "aliases": ["c"]}]}
    with pytest.raises(LLMTransportError, match="'a', 'b', 'c'"):
        _probe(many)

    # A server is listening but cannot list models: the real call reports any problem.
    _probe(error=urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b"")))  # type: ignore[arg-type]
    _probe(error=urllib.error.HTTPError("u", 500, "Error", {}, io.BytesIO(b"")))  # type: ignore[arg-type]
    # A single-model server answers under any requested name.
    _probe({"data": [{"id": "/models/some-file.gguf"}]})
    # Listed by alias (llama.cpp router presets).
    _probe({"data": [{"id": "a"}, {"id": "b", "aliases": ["wanted"]}]})
    _probe({"data": [{"id": "a"}, {"id": "wanted"}]})
