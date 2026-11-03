"""The single LLM seam (`ctx.llm`).

Recipes reach language models only through `ctx.llm`, which is `None` when a stage does
not declare `Stage.params["model"]`. Provider SDKs (`pydantic_ai`, `openai`, `anthropic`)
are never imported at module load time or when `ctx.llm` is `None`.

Invariants enforced here:
- Egress gate: local targets are `ollama:<model>` (on localhost) or a URL on
  `localhost` / `127.0.0.1` / `::1`. Every other target is remote and is refused at
  stage startup unless `params["allow_remote"] is True`.
- Keys from environment only (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`). Core never reads a `.env` file. Missing keys fail at stage startup.
- No silent truncation: inputs are measured before calling the transport; exceeding the
  model context minus reserve raises `InputTooLarge` and records `status='refused'`. The
  context window is declared per stage (`params["context_tokens"]`, required with `model`):
  core keeps no model table, because a stale or guessed window is a silent default.
- Cache before validation: raw responses are written to `llm_cache` before coercion and
  schema validation so editing a schema or validator never re-calls the model.
- Tolerant coercion, strict schema validation, no silent defaults: common formatting
  faults (`fenced_json`, `trailing_prose`, `single_quotes`, `trailing_commas`,
  `string_numbers`, `null_for_missing_optional`) are repaired and recorded. Only these
  six: any other lax coercion (`1` for a bool, an ISO string for a date) is rejected by
  Pydantic strict mode. JSON has one number type, so an integer is a valid `float`. If
  strict validation (`extra="forbid"`) still fails, `ExtractionFailed` is raised with
  `.raw` attached.
- Retries and circuit breaker: only transient transport faults (`timeout`, connection
  errors, HTTP 429 and 5xx) retry with exponential backoff under `retry_budget_s`
  (default 120s). Validation failures never retry. After `max_consecutive_failures`
  (default 5) consecutive `ExtractionFailed` in one run, `SystematicModelFailure`
  (`model_systematic_failure`) is raised to abort the run.
- Fail fast on a dead local server: before the first call in a run that misses
  `llm_cache`, `probe_local_model` checks the server once (no retries), so nothing
  listening fails in seconds, naming the URL, instead of after the retry budget.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import types
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol, Union, get_args, get_origin
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError

from commissure.workspace import _CONN_LOCK, transaction, utc_now

# A server that never accepts a connection is not a mid-call drop: probe once with a short
# timeout and no `retry_budget_s`. Transient retries (default 120s) still apply after a
# call has started and then fails.
LOCAL_MODEL_PROBE_TIMEOUT_S = 3.0

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

PROVIDER_ENV_KEYS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

DEFAULT_RESERVE_TOKENS = 1_024
DEFAULT_RETRY_BUDGET_S = 120.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

PROVIDER_DEFAULT_BASE_URLS: dict[str, str] = {
    "ollama": "http://localhost:11434/v1",
    "llamacpp": "http://localhost:8080/v1",
}

CONTROL_PARAMS = frozenset(
    {
        "model",
        "allow_remote",
        "base_url",
        "context_tokens",
        "reserve_tokens",
        "retry_budget_s",
        "max_consecutive_failures",
        "timeout_s",
    }
)

# Only these keys reach the model. Stage params also carry recipe settings (a chunk budget
# read via `ctx.params`); those still enter the cache key with the sampling keys, because
# that key composition is part of the workspace format and changing it orphans `llm_cache`.
SAMPLING_PARAMS = (
    "temperature",
    "top_p",
    "max_tokens",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "reasoning_effort",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    text = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Public Types & Exceptions ----------------------------------------------------


class Part(NamedTuple):
    label: str
    text: str


@dataclass(frozen=True)
class Extraction[T: BaseModel]:
    payload: T
    raw: str
    repairs: list[str]
    cached: bool
    call_id: int


@dataclass(frozen=True)
class Completion:
    text: str
    cached: bool
    call_id: int


class LLMError(Exception):
    """Base class for LLM seam errors."""


class LLMConfigError(LLMError, ValueError):
    """Invalid model specification, disallowed remote target, or missing API key."""


class RemoteModelRefused(LLMConfigError):
    """Remote provider requested without `allow_remote=True` in stage params."""


class MissingAPIKeyError(LLMConfigError):
    """Required provider API key is missing from the environment."""


class InputTooLarge(LLMError):
    """Prompt and schema exceed the model's context window minus the output reserve."""

    def __init__(self, message: str, *, input_tokens: int, limit_tokens: int) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.limit_tokens = limit_tokens


class ExtractionFailed(LLMError):
    """Model output could not be coerced and validated against the recipe schema."""

    def __init__(
        self,
        message: str,
        *,
        raw: str,
        repairs: list[str] | None = None,
        call_id: int | None = None,
        truncated: bool = False,
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.repairs = list(repairs or [])
        self.call_id = call_id
        self.truncated = truncated


class LLMTransportError(LLMError):
    """Transport call failed (after any transient retries)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transient: bool = False,
        call_id: int | None = None,
        attempts: int = 1,
        subject_key: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        self.call_id = call_id
        self.attempts = attempts
        self.subject_key = subject_key


class SystematicModelFailure(LLMError):
    """Circuit breaker tripped after N consecutive ExtractionFailed outcomes."""


# --- Target Parsing & Startup Validation ------------------------------------------


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str
    base_url: str | None
    is_local: bool
    required_env_key: str | None


def _is_localhost_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return (parsed.hostname or "").lower() in LOCAL_HOSTS


def _normalize_openai_base_url(url: str) -> str:
    stripped = url.rstrip("/")
    parsed = urlparse(stripped)
    if parsed.path in ("", "/"):
        return f"{stripped}/v1"
    return stripped


def parse_model_spec(spec: str, params: Mapping[str, Any] | None = None) -> ModelTarget:
    """Parse a `provider:model` specification (plus optional `params["base_url"]`)."""
    cfg = dict(params or {})
    if not isinstance(spec, str) or not spec.strip():
        raise LLMConfigError("stage parameter 'model' must be a non-empty string")
    spec = spec.strip()
    if ":" not in spec:
        raise LLMConfigError(
            f"invalid model {spec!r}; expected 'provider:model' "
            "(e.g. 'ollama:qwen2.5:7b', 'openrouter:anthropic/claude-sonnet')"
        )
    raw_provider, rest = spec.split(":", 1)
    provider = raw_provider.strip().lower()
    model = rest.strip()
    if not model:
        raise LLMConfigError(f"invalid model {spec!r}; model name after '{raw_provider}:' is empty")
    if provider not in ("ollama", "llamacpp", *PROVIDER_ENV_KEYS):
        raise LLMConfigError(
            f"unknown LLM provider {provider!r} in {spec!r}; "
            "supported providers: ollama, llamacpp, openrouter, anthropic, openai"
        )

    raw_base = cfg.get("base_url") or PROVIDER_DEFAULT_BASE_URLS.get(provider)
    base_url: str | None = None
    if raw_base:
        # The Anthropic SDK appends `/v1/messages` itself; OpenAI-compatible clients expect `/v1`.
        base_url = (
            str(raw_base).rstrip("/")
            if provider == "anthropic"
            else _normalize_openai_base_url(str(raw_base))
        )
    is_local = _is_localhost_url(base_url)
    required_key = PROVIDER_ENV_KEYS.get(provider)
    if provider == "openai" and is_local:
        required_key = None
    return ModelTarget(
        provider=provider,
        model=model,
        base_url=base_url,
        is_local=is_local,
        required_env_key=required_key,
    )


def validate_stage_llm(params: Mapping[str, Any] | None) -> ModelTarget | None:
    """Validate LLM configuration at stage startup before any network or run activity.

    Returns `None` when no model is configured. Raises `RemoteModelRefused` if a
    remote provider is configured without `allow_remote=True`, or `MissingAPIKeyError`
    if the required provider API key is absent from `os.environ`.
    """
    cfg = dict(params or {})
    spec = cfg.get("model")
    if spec is None:
        return None
    target = parse_model_spec(str(spec), cfg)
    allow_remote = cfg.get("allow_remote") is True
    if not target.is_local and not allow_remote:
        raise RemoteModelRefused(
            f"remote model '{target.provider}:{target.model}' is refused unless "
            "Stage.params['allow_remote'] is True"
        )
    if target.required_env_key is not None:
        val = os.environ.get(target.required_env_key, "").strip()
        if not val:
            raise MissingAPIKeyError(
                f"provider '{target.provider}' requires environment variable "
                f"{target.required_env_key} to be set"
            )
    context = cfg.get("context_tokens")
    if context is None:
        raise LLMConfigError(
            "stage parameter 'context_tokens' is required with 'model' "
            "(the model's context window in tokens)"
        )
    if isinstance(context, bool) or not isinstance(context, int) or context <= 0:
        raise LLMConfigError(f"context_tokens must be a positive integer, got {context!r}")
    return target


def probe_local_model(
    target: ModelTarget,
    *,
    timeout_s: float = LOCAL_MODEL_PROBE_TIMEOUT_S,
    urlopen: Any = None,
) -> None:
    """Fail fast when nothing answers at a local model server, or it lists other models.

    One `GET {base_url}/models` with a short timeout and no retry budget. Remote
    targets are skipped: the measured failure was a local server with nothing
    listening, and probing a remote host would send an API key on every run.

    Only a refused or timed-out connection, or a listing of several models that
    names neither the model nor an alias, fails. Any HTTP error status means a
    server is listening (some do not implement `/models`), and a single listed
    model passes because single-model servers such as `llama-server` answer under
    any requested name; the real call reports anything else without retrying.
    """
    if not target.is_local:
        return
    if not target.base_url:
        raise LLMConfigError(
            f"local model '{target.provider}:{target.model}' has no base_url to probe"
        )
    models_url = f"{target.base_url.rstrip('/')}/models"
    req = urllib.request.Request(
        models_url,
        headers={"Accept": "application/json", "User-Agent": "commissure-model-probe/1.0"},
        method="GET",
    )
    opener = urlopen if urlopen is not None else urllib.request.urlopen
    try:
        with opener(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError:
        return
    except Exception as exc:
        raise LLMTransportError(
            f"local model server at {models_url} is not reachable "
            f"({type(exc).__name__}: {exc}); expected model {target.model!r}",
            transient=False,
        ) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return
    entries = [e for e in data if isinstance(e, dict)]
    names: list[str] = []
    for entry in entries:
        if isinstance(entry.get("id"), str):
            names.append(entry["id"])
        aliases = entry.get("aliases")
        if isinstance(aliases, list):
            names.extend(a for a in aliases if isinstance(a, str))
    if len(entries) <= 1 or target.model in names:
        return
    listed = ", ".join(repr(n) for n in names) if names else "(none)"
    raise LLMTransportError(
        f"local model server at {models_url} does not list model {target.model!r}; "
        f"available: {listed}",
        transient=False,
    )


# --- Token Estimation & Context Window Table --------------------------------------


def estimate_tokens(text: str) -> int:
    """Deterministic, tiktoken-free token estimate: ceil(utf8_bytes / 4).

    tiktoken would pull in requests and certifi (MPL-2.0); the estimate errs high on
    multi-byte text, and the ledger records the provider's exact counts when it has them.
    """
    if not text:
        return 0
    return math.ceil(len(text.encode("utf-8")) / 4)


def resolve_reserve_tokens(context_tokens: int, params: Mapping[str, Any] | None = None) -> int:
    if params and params.get("reserve_tokens") is not None:
        return max(0, int(params["reserve_tokens"]))
    return min(DEFAULT_RESERVE_TOKENS, max(16, context_tokens // 4))


def normalize_parts(parts: Sequence[Part | tuple[str, str]]) -> tuple[Part, ...]:
    out: list[Part] = []
    for p in parts:
        if isinstance(p, Part):
            out.append(p)
        elif isinstance(p, tuple) and len(p) == 2:
            out.append(Part(label=str(p[0]), text=str(p[1])))
        else:
            raise TypeError(f"expected Part(label, text) or (label, text), got {p!r}")
    return tuple(out)


def format_parts(parts: Sequence[Part | tuple[str, str]]) -> str:
    norm = normalize_parts(parts)
    return "\n\n".join(f"[{p.label}]\n{p.text}" for p in norm)


# --- Secret Redaction -------------------------------------------------------------


def _secret_values() -> list[str]:
    secrets: list[str] = []
    for k, v in os.environ.items():
        if not v or len(v.strip()) < 4:
            continue
        upper = k.upper()
        if (
            upper in PROVIDER_ENV_KEYS.values()
            or upper.endswith("_API_KEY")
            or upper.endswith("_SECRET")
            or upper.endswith("_TOKEN")
        ):
            secrets.append(v.strip())
    return sorted(set(secrets), key=len, reverse=True)


def _scrub_secrets(text: str | None) -> str | None:
    if not text:
        return text
    out = text
    for secret in _secret_values():
        if secret in out:
            out = out.replace(secret, "[REDACTED]")
    return out


# --- Transport Protocol & PydanticAI Driver ---------------------------------------


@dataclass(frozen=True)
class TransportRequest:
    provider: str
    model: str
    base_url: str | None
    system: str
    parts: tuple[Part, ...]
    user_message: str
    schema: dict[str, Any] | None
    sampling_params: dict[str, Any]
    timeout_s: float = 60.0


@dataclass(frozen=True)
class TransportResponse:
    raw_text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class Transport(Protocol):
    def request(self, req: TransportRequest) -> TransportResponse: ...


_DEFAULT_TRANSPORT: Transport | None = None


@contextmanager
def use_transport(transport: Transport) -> Iterator[Transport]:
    """Context manager to temporarily replace the default LLM transport in tests."""
    global _DEFAULT_TRANSPORT
    prev = _DEFAULT_TRANSPORT
    _DEFAULT_TRANSPORT = transport
    try:
        yield transport
    finally:
        _DEFAULT_TRANSPORT = prev


_TRANSIENT_TIMEOUT_TYPES = frozenset(
    {
        "TimeoutError",
        "APITimeoutError",
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ModelTimeoutError",
        "ModelRequestTimeout",
    }
)

_TRANSIENT_CONNECTION_TYPES = frozenset(
    {
        "ConnectionError",
        "APIConnectionError",
        "ConnectError",
        "NetworkError",
        "RemoteProtocolError",
        "RemoteDisconnected",
        "ProtocolError",
        "ModelConnectionError",
    }
)

_TRANSIENT_STATUS_TYPES = frozenset(
    {
        "RateLimitError",
        "InternalServerError",
        "ModelRateLimitError",
        "ModelUnavailableError",
        "ModelOverloadedError",
        "ModelServerError",
    }
)


def _classify_transport_exception(exc: BaseException) -> LLMTransportError:
    msg = _scrub_secrets(f"{type(exc).__name__}: {exc}") or type(exc).__name__
    status_code: int | None = None
    is_timeout = False
    is_conn = False
    is_rate_or_5xx = False

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if status_code is None:
            code = getattr(cur, "status_code", None)
            if not isinstance(code, int):
                resp = getattr(cur, "response", None)
                code = getattr(resp, "status_code", None) if resp is not None else None
            if not isinstance(code, int) and isinstance(getattr(cur, "body", None), dict):
                raw_code = cur.body.get("status_code")
                if isinstance(raw_code, int):
                    code = raw_code
            if isinstance(code, int):
                status_code = code

        mro_names = {cls.__name__ for cls in type(cur).__mro__}
        if isinstance(cur, TimeoutError) or (mro_names & _TRANSIENT_TIMEOUT_TYPES):
            is_timeout = True
        if isinstance(cur, ConnectionError) or (mro_names & _TRANSIENT_CONNECTION_TYPES):
            is_conn = True
        if mro_names & _TRANSIENT_STATUS_TYPES:
            is_rate_or_5xx = True

        cur = cur.__cause__ or cur.__context__

    if status_code == 429 or (status_code is not None and 500 <= status_code < 600):
        is_rate_or_5xx = True

    if "peg-native format" in msg.lower():
        # `llama.cpp` returns HTTP 500 when a reasoning model's generated text fails
        # Harmony PEG grammar framing (e.g. thinking trace truncated at `max_tokens`).
        # Retrying identical sampling parameters in the transport loop repeats the same
        # generation; surface immediately so the caller can adjust parameters or quarantine.
        transient = False
    else:
        transient = bool(is_timeout or is_conn or is_rate_or_5xx)
    return LLMTransportError(msg, status_code=status_code, transient=transient)


class PydanticAITransport:
    """Default driver using `pydantic-ai` (`pydantic_ai.direct.model_request`).

    Imports `pydantic_ai` and provider classes lazily inside `request()` so importing
    `commissure` never imports any provider SDK when `ctx.llm` is unused. The dependency
    is `pydantic-ai-slim[anthropic,openrouter]`: full `pydantic-ai` pulls about 100
    packages, and the `openai` extra pulls tiktoken and certifi (MPL-2.0). Retries,
    caching, and repair belong to this seam, so SDK retries are off (`max_retries=0`).
    """

    def request(self, req: TransportRequest) -> TransportResponse:
        try:
            return asyncio.run(self._request_async(req))
        except LLMTransportError:
            raise
        except Exception as exc:
            raise _classify_transport_exception(exc) from exc

    async def _request_async(self, req: TransportRequest) -> TransportResponse:
        from pydantic_ai.direct import model_request
        from pydantic_ai.messages import ModelRequest, SystemPromptPart, TextPart, UserPromptPart
        from pydantic_ai.settings import ModelSettings

        system_text = req.system
        if req.schema is not None:
            schema_str = _canonical(req.schema)
            system_text = (
                f"{system_text}\n\n"
                "Return ONLY a single valid JSON object conforming strictly to this JSON Schema "
                "(no markdown fences, no extra fields, no prose):\n"
                f"{schema_str}"
            ).strip()

        settings_dict: dict[str, Any] = {"timeout": req.timeout_s}
        settings_dict.update(req.sampling_params)
        extra_body: dict[str, Any] = dict(settings_dict.get("extra_body") or {})
        if "reasoning_effort" in settings_dict:
            effort = settings_dict.pop("reasoning_effort")
            if effort is not None:
                extra_body["reasoning_effort"] = effort
        if (
            req.schema is not None
            and req.provider in ("llamacpp", "openai-compatible")
            and "response_format" not in extra_body
        ):
            # Enable `llama.cpp`'s native JSON/Harmony PEG grammar constraint at token-sampling
            # time so reasoning models (`gpt-oss-20b`) cannot emit malformed channel transitions
            # (`HTTP 500 peg-native format`) when closing the analysis channel.
            extra_body["response_format"] = {"type": "json_object"}
        if extra_body:
            settings_dict["extra_body"] = extra_body
        if req.schema is not None and "temperature" not in settings_dict:
            settings_dict["temperature"] = 0.0

        model, client = self._build_model(req)
        async with client:
            messages = [
                ModelRequest(
                    parts=[
                        SystemPromptPart(content=system_text),
                        UserPromptPart(content=req.user_message),
                    ]
                )
            ]
            response = await model_request(
                model,
                messages,
                model_settings=ModelSettings(**settings_dict),
                instrument=False,
            )

        text_chunks = [p.content for p in response.parts if isinstance(p, TextPart)]
        raw_text = "".join(text_chunks) if text_chunks else (response.text or "")
        usage = response.usage
        in_tokens = usage.input_tokens if usage and usage.input_tokens is not None else None
        out_tokens = usage.output_tokens if usage and usage.output_tokens is not None else None

        cost_usd: float | None = None
        if req.provider in ("openrouter", "anthropic", "openai") and not _is_localhost_url(
            req.base_url
        ):
            try:
                price = response.cost()
                if price is not None and price.total_price is not None:
                    cost_usd = float(price.total_price)
            except Exception:
                cost_usd = None

        return TransportResponse(
            raw_text=raw_text,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=cost_usd,
        )

    def _build_model(self, req: TransportRequest) -> tuple[Any, Any]:
        if req.provider == "ollama":
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.ollama import OllamaProvider

            client = AsyncOpenAI(
                base_url=req.base_url or "http://localhost:11434/v1",
                api_key="ollama",
                max_retries=0,
                timeout=req.timeout_s,
            )
            return OpenAIChatModel(req.model, provider=OllamaProvider(openai_client=client)), client

        if req.provider in ("llamacpp", "openai-compatible"):
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            client = AsyncOpenAI(
                base_url=req.base_url or "http://localhost:8080/v1",
                api_key=os.environ.get("OPENAI_API_KEY") or "local",
                max_retries=0,
                timeout=req.timeout_s,
            )
            return OpenAIChatModel(req.model, provider=OpenAIProvider(openai_client=client)), client

        if req.provider == "openrouter":
            from openai import AsyncOpenAI
            from pydantic_ai.models.openrouter import OpenRouterModel
            from pydantic_ai.providers.openrouter import OpenRouterProvider

            api_key = os.environ["OPENROUTER_API_KEY"]
            client = AsyncOpenAI(
                base_url=req.base_url or "https://openrouter.ai/api/v1",
                api_key=api_key,
                max_retries=0,
                timeout=req.timeout_s,
            )
            return (
                OpenRouterModel(req.model, provider=OpenRouterProvider(openai_client=client)),
                client,
            )

        if req.provider == "anthropic":
            from anthropic import AsyncAnthropic
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            api_key = os.environ["ANTHROPIC_API_KEY"]
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "max_retries": 0,
                "timeout": req.timeout_s,
            }
            if req.base_url:
                kwargs["base_url"] = req.base_url
            client = AsyncAnthropic(**kwargs)
            return (
                AnthropicModel(req.model, provider=AnthropicProvider(anthropic_client=client)),
                client,
            )

        if req.provider == "openai":
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            api_key = os.environ.get("OPENAI_API_KEY") or "local"
            kwargs = {
                "api_key": api_key,
                "max_retries": 0,
                "timeout": req.timeout_s,
            }
            if req.base_url:
                kwargs["base_url"] = req.base_url
            client = AsyncOpenAI(**kwargs)
            return OpenAIChatModel(req.model, provider=OpenAIProvider(openai_client=client)), client

        raise LLMConfigError(f"unsupported provider {req.provider!r}")


# --- Coercion & Repair Boundary ---------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _strip_fences_and_prose(raw: str, repairs: list[str]) -> str:
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m is not None:
        prefix = text[: m.start()].strip()
        suffix = text[m.end() :].strip()
        if "fenced_json" not in repairs:
            repairs.append("fenced_json")
        if (prefix or suffix) and "trailing_prose" not in repairs:
            repairs.append("trailing_prose")
        text = m.group(1).strip()

    span = _find_balanced_json_span(text)
    if span is not None:
        start, end = span
        before = text[:start].strip()
        after = text[end:].strip()
        if before or after:
            if "trailing_prose" not in repairs:
                repairs.append("trailing_prose")
            text = text[start:end]
    return text


def _find_balanced_json_span(text: str) -> tuple[int, int] | None:
    start = -1
    for i, ch in enumerate(text):
        if ch in ("{", "["):
            start = i
            break
    if start == -1:
        return None

    stack: list[str] = []
    in_string: str | None = None
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string is not None:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in ('"', "'"):
            in_string = ch
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()
            if not stack:
                return start, i + 1
    return None


def _remove_trailing_commas(text: str) -> tuple[str, bool]:
    """Remove commas immediately preceding `}` or `]` outside of string literals."""
    out: list[str] = []
    in_string: str | None = None
    escape = False
    changed = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string is not None:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_string = ch
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in (" ", "\t", "\r", "\n"):
                j += 1
            if j < n and text[j] in ("}", "]"):
                changed = True
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out), changed


def _convert_single_quotes(text: str) -> tuple[str, bool]:
    """Convert single-quoted string literals `'...'` to double-quoted JSON strings."""
    out: list[str] = []
    changed = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            out.append(ch)
            i += 1
            while i < n:
                c = text[i]
                out.append(c)
                i += 1
                if c == "\\" and i < n:
                    out.append(text[i])
                    i += 1
                elif c == '"':
                    break
            continue
        if ch == "'":
            changed = True
            out.append('"')
            i += 1
            while i < n:
                c = text[i]
                i += 1
                if c == "\\" and i < n:
                    nxt = text[i]
                    i += 1
                    if nxt == "'":
                        out.append("'")
                    elif nxt == '"':
                        out.append('\\"')
                    else:
                        out.append("\\" + nxt)
                elif c == '"':
                    out.append('\\"')
                elif c == "'":
                    out.append('"')
                    break
                else:
                    out.append(c)
            continue
        out.append(ch)
        i += 1
    return "".join(out), changed


def _annotation_parts(tp: Any) -> tuple[set[Any], list[type[BaseModel]], Any]:
    """Inspect a field type annotation: returns (scalar_types, model_types, list_item_type)."""
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        scalars: set[Any] = set()
        models: list[type[BaseModel]] = []
        list_item: Any = None
        for arg in get_args(tp):
            s, m, li = _annotation_parts(arg)
            scalars |= s
            models.extend(m)
            if li is not None:
                list_item = li
        return scalars, models, list_item
    if origin is list or origin is Sequence or origin is tuple:
        args = get_args(tp)
        return {list}, [], (args[0] if args else None)
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return set(), [tp], None
    return {tp}, [], None


def _coerce_string_number(val: str, scalars: set[Any]) -> tuple[Any, bool]:
    if str in scalars:
        return val, False
    stripped = val.strip().replace(",", "")
    if not stripped:
        return val, False
    if int in scalars:
        try:
            return int(stripped), True
        except ValueError:
            pass
    if float in scalars:
        try:
            parsed = float(stripped)
            if math.isfinite(parsed):
                return parsed, True
        except ValueError:
            pass
    return val, False


def _coerce_value_for_type(val: Any, tp: Any, repairs: list[str]) -> Any:
    scalars, models, list_item = _annotation_parts(tp)
    if isinstance(val, str) and (int in scalars or float in scalars) and str not in scalars:
        coerced, did = _coerce_string_number(val, scalars)
        if did:
            if "string_numbers" not in repairs:
                repairs.append("string_numbers")
            return coerced
    if isinstance(val, dict) and models:
        return _coerce_dict_for_schema(val, models[0], repairs)
    if isinstance(val, list) and list_item is not None:
        return [_coerce_value_for_type(item, list_item, repairs) for item in val]
    return val


def _coerce_dict_for_schema(
    data: dict[str, Any], schema: type[BaseModel], repairs: list[str]
) -> dict[str, Any]:
    out = dict(data)
    for field_name, field_info in schema.model_fields.items():
        scalars, _, _ = _annotation_parts(field_info.annotation)
        allows_none = type(None) in scalars

        if field_name in out and out[field_name] is None:
            if not allows_none and not field_info.is_required():
                # Model emitted null for an optional field that has a non-null default.
                del out[field_name]
                if "null_for_missing_optional" not in repairs:
                    repairs.append("null_for_missing_optional")
                continue
        elif field_name not in out and allows_none and field_info.is_required():
            # Field is typed `T | None` (nullable optional) without `= None` default and
            # was omitted by the model.
            out[field_name] = None
            if "null_for_missing_optional" not in repairs:
                repairs.append("null_for_missing_optional")
            continue

        if field_name in out and out[field_name] is not None:
            out[field_name] = _coerce_value_for_type(
                out[field_name], field_info.annotation, repairs
            )
    return out


def _is_truncated_json(raw: str) -> bool:
    """True when `raw` opens a JSON object or array whose balanced delimiters never close."""
    stripped = raw.strip()
    if not stripped:
        return False
    m = _FENCE_RE.search(stripped)
    if m is not None:
        candidate = m.group(1).strip()
    elif stripped.startswith("```"):
        nl = stripped.find("\n")
        candidate = stripped[nl + 1 :].strip() if nl != -1 else ""
    else:
        candidate = stripped
    if not any(ch in candidate for ch in ("{", "[")):
        return False
    return _find_balanced_json_span(candidate) is None


def coerce_and_validate[T: BaseModel](raw: str, schema: type[T]) -> tuple[T, list[str]]:
    """Apply the 6-step tolerant repair pass, then strictly validate against `schema`.

    Raises `ExtractionFailed` with `.raw` and `.repairs` attached if parsing or
    validation fails. Never substitutes fallback defaults for required fields.
    """
    if schema.model_config.get("extra") != "forbid":
        raise LLMConfigError(f"schema {schema.__name__} must set extra='forbid'")

    repairs: list[str] = []
    text = _strip_fences_and_prose(raw, repairs)
    text, did_commas = _remove_trailing_commas(text)
    if did_commas and "trailing_commas" not in repairs:
        repairs.append("trailing_commas")

    parsed: Any = None
    parsed_ok = False
    try:
        parsed = json.loads(text)
        parsed_ok = True
    except Exception:
        converted, did_quotes = _convert_single_quotes(text)
        if did_quotes:
            converted, did_commas_2 = _remove_trailing_commas(converted)
            try:
                parsed = json.loads(converted)
                parsed_ok = True
                if "single_quotes" not in repairs:
                    repairs.append("single_quotes")
                if did_commas_2 and "trailing_commas" not in repairs:
                    repairs.append("trailing_commas")
            except Exception:
                try:
                    parsed = ast.literal_eval(text)
                    parsed_ok = True
                    if "single_quotes" not in repairs:
                        repairs.append("single_quotes")
                except Exception:
                    pass

    if not parsed_ok or not isinstance(parsed, dict):
        is_trunc = _is_truncated_json(raw)
        raise ExtractionFailed(
            "model_output_invalid: response is not a valid JSON object",
            raw=raw,
            repairs=repairs,
            truncated=is_trunc,
        )

    coerced = _coerce_dict_for_schema(parsed, schema, repairs)
    try:
        validated = schema.model_validate(coerced, strict=True)
    except ValidationError as exc:
        raise ExtractionFailed(
            f"model_output_invalid: {exc}",
            raw=raw,
            repairs=repairs,
        ) from exc

    return validated, repairs


# --- Per-Key Cache Lock -------------------------------------------------------------

# Identical requests in flight (form-letter comments chunk to the same text) wait for the
# first call and then hit `llm_cache` instead of calling the model twice. Locks are striped
# so a long-lived `commissure serve` process holds a fixed set rather than one per prompt
# ever seen; two different keys sharing a stripe only serialize, never mix.
_KEY_LOCKS = tuple(threading.Lock() for _ in range(256))


def _key_lock(cache_key: str) -> threading.Lock:
    return _KEY_LOCKS[int(cache_key[:8], 16) % len(_KEY_LOCKS)]


# --- The Seam (`ctx.llm`) ---------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    """One prepared request: what to send, how it is keyed, and how it is ledgered."""

    target: ModelTarget
    effective: dict[str, Any]
    system: str
    parts: tuple[Part, ...]
    user_message: str
    schema_json: dict[str, Any] | None
    sampling: dict[str, Any]
    subject_key: str
    cache_key: str
    request_json: str
    request_hash: str
    prompt_hash: str
    est_input_tokens: int


@dataclass(frozen=True)
class _Response:
    raw_text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int
    cached: bool


@dataclass
class LlmSeam:
    """Stage-scoped LLM seam exposed as `ctx.llm`."""

    conn: sqlite3.Connection
    run_id: int
    stage: str
    stage_params: Mapping[str, Any]
    no_cache: bool = False
    transport: Transport | None = None
    subject_key_getter: Callable[[], str] = lambda: "run"
    on_before_call: Callable[[str], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    breaker_error: SystematicModelFailure | None = field(default=None, init=False)
    _probe_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _probed: dict[tuple[str, str], LLMTransportError | None] = field(
        default_factory=dict, init=False
    )

    def __post_init__(self) -> None:
        if validate_stage_llm(self.stage_params) is None:
            raise LLMConfigError("LlmSeam requires Stage.params['model'] to be set")

    def _active_transport(self) -> Transport:
        if self.transport is not None:
            return self.transport
        if _DEFAULT_TRANSPORT is not None:
            return _DEFAULT_TRANSPORT
        return PydanticAITransport()

    def _ensure_server(self, target: ModelTarget, transport: Transport) -> None:
        """Probe a local server once per run, before the first call that misses the cache.

        Lazy, so a run served entirely from `llm_cache` never needs the server. Only the
        real HTTP transport is probed: an injected transport contacts no server. The
        first result is kept, so other workers fail at once instead of probing again.
        """
        if not target.is_local or not isinstance(transport, PydanticAITransport):
            return
        key = (target.base_url or "", target.model)
        with self._probe_lock:
            if key not in self._probed:
                try:
                    probe_local_model(target)
                    self._probed[key] = None
                except LLMTransportError as exc:
                    self._probed[key] = exc
            err = self._probed[key]
        if err is not None:
            raise LLMTransportError(str(err), transient=False)

    def extract[T: BaseModel](
        self,
        schema: type[T],
        *,
        system: str,
        parts: Sequence[Part | tuple[str, str]],
        params: Mapping[str, Any] | None = None,
    ) -> Extraction[T]:
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise LLMConfigError(
                f"extract() requires a Pydantic BaseModel subclass, got {schema!r}"
            )
        if schema.model_config.get("extra") != "forbid":
            raise LLMConfigError(f"schema {schema.__name__} must set extra='forbid'")

        call = self._prepare("extract", schema.model_json_schema(), system, parts, params)
        resp = self._fetch_or_call(call)
        try:
            # Reasoning models that exhaust their output budget in a thinking trace return
            # empty final text alongside a high `output_tokens` count; surface that cause
            # explicitly instead of a generic "empty model output".
            if not resp.raw_text.strip() and resp.output_tokens > 0:
                raise ExtractionFailed(
                    f"model_output_invalid: model returned empty final text after generating"
                    f" {resp.output_tokens} output tokens (reasoning/thinking tokens likely"
                    f" exhausted generation before emitting JSON)",
                    raw=resp.raw_text,
                    repairs=[],
                    truncated=True,
                )
            validated, repairs = coerce_and_validate(resp.raw_text, schema)
        except ExtractionFailed as exc:
            exc.call_id = self._record_response(
                call, resp, status="invalid", repairs_count=len(exc.repairs), error=str(exc)
            )
            self._on_validation_failure(call.effective, exc)
            raise

        self._on_validation_success()
        call_id = self._record_response(call, resp, status="ok", repairs_count=len(repairs))
        return Extraction(
            payload=validated,
            raw=resp.raw_text,
            repairs=repairs,
            cached=resp.cached,
            call_id=call_id,
        )

    def extract_chunks[T: BaseModel](
        self,
        schema: type[T],
        chunks: Sequence[Any],
        *,
        system: str,
        parts_fn: Callable[[Any], Sequence[Part | tuple[str, str]]] | None = None,
        params: Mapping[str, Any] | None = None,
        retry_params: Mapping[str, Any] | None = None,
        min_chunk_tokens: int = 400,
    ) -> list[tuple[Any, Extraction[T]]]:
        """Extract `schema` over each `TextChunk`, subdividing chunks on failure.

        When a chunk raises `InputTooLarge`, `ExtractionFailed`, or a `peg-native format`
        `LLMTransportError` (after an optional `retry_params` attempt) and its token count
        exceeds `min_chunk_tokens`, the chunk is split in half via `chunk.split_half()`
        (preserving exact `[start, end)` character offsets and structural metadata) and
        each half is extracted recursively.
        """
        from commissure.chunk import TextChunk, chunk_text

        norm_chunks: list[TextChunk] = []
        total_initial = len(chunks)
        for idx, ch in enumerate(chunks, start=1):
            if isinstance(ch, TextChunk):
                norm_chunks.append(ch)
            elif isinstance(ch, str):
                built = chunk_text(ch, budget_tokens=max(1, estimate_tokens(ch)))
                if built:
                    norm_chunks.append(
                        TextChunk(
                            text=built[0].text,
                            start=built[0].start,
                            end=built[0].end,
                            index=idx,
                            total=total_initial,
                        )
                    )
            else:
                raise TypeError(f"expected TextChunk or str, got {ch!r}")

        def _default_parts(c: TextChunk) -> Sequence[Part]:
            return (Part("chunk_position", c.context_label()), Part("text", c.text))

        build_parts = parts_fn if parts_fn is not None else _default_parts
        floor = max(1, int(min_chunk_tokens))

        def _run_one(c: TextChunk) -> list[tuple[TextChunk, Extraction[T]]]:
            with self._lock:
                saved_failures = self._consecutive_failures
                saved_breaker = self.breaker_error

            parts = build_parts(c)
            last_exc: Exception | None = None
            skip_retry = False
            try:
                ext = self.extract(schema, system=system, parts=parts, params=params)
                return [(c, ext)]
            except InputTooLarge as exc:
                last_exc = exc
                skip_retry = True
            except ExtractionFailed as exc:
                last_exc = exc
            except LLMTransportError as exc:
                if "peg-native format" not in str(exc).lower():
                    raise
                last_exc = exc

            if not skip_retry and retry_params is not None:
                merged_retry = {**(params or {}), **retry_params}
                try:
                    ext = self.extract(schema, system=system, parts=parts, params=merged_retry)
                    return [(c, ext)]
                except (InputTooLarge, ExtractionFailed) as exc:
                    last_exc = exc
                except LLMTransportError as exc:
                    if "peg-native format" not in str(exc).lower():
                        raise
                    last_exc = exc

            if c.tokens > floor:
                sub_chunks = c.split_half()
                if len(sub_chunks) > 1 and all(sc.tokens < c.tokens for sc in sub_chunks):
                    # Subdividing an oversized or reasoning-heavy chunk is adaptive recovery,
                    # not a terminal leaf failure; restore the circuit-breaker counter.
                    with self._lock:
                        self._consecutive_failures = saved_failures
                        self.breaker_error = saved_breaker
                    out_sub: list[tuple[TextChunk, Extraction[T]]] = []
                    for sc in sub_chunks:
                        out_sub.extend(_run_one(sc))
                    return out_sub

            assert last_exc is not None
            raise last_exc

        results: list[tuple[TextChunk, Extraction[T]]] = []
        for c in norm_chunks:
            results.extend(_run_one(c))
        return results

    def complete(
        self,
        *,
        system: str,
        parts: Sequence[Part | tuple[str, str]],
        params: Mapping[str, Any] | None = None,
    ) -> Completion:
        call = self._prepare("complete", None, system, parts, params)
        resp = self._fetch_or_call(call)
        call_id = self._record_response(call, resp, status="ok", repairs_count=0)
        return Completion(text=resp.raw_text, cached=resp.cached, call_id=call_id)

    def _prepare(
        self,
        mode: str,
        schema_json: dict[str, Any] | None,
        system: str,
        parts: Sequence[Part | tuple[str, str]],
        call_params: Mapping[str, Any] | None,
    ) -> _Call:
        """Merge params, re-check the egress gate, key the request, and enforce the budget."""
        effective = {**self.stage_params, **(call_params or {})}
        target = validate_stage_llm(effective)
        assert target is not None  # `model` cannot be removed by a per-call override
        norm_parts = normalize_parts(parts)
        user_message = format_parts(norm_parts)
        sampling = {k: v for k, v in effective.items() if k not in CONTROL_PARAMS}
        raw_parts = [[p.label, p.text] for p in norm_parts]

        # The cache key payload is part of the workspace format: changing it orphans every
        # cached response, so keep its keys and shape stable.
        cache_key = _sha256(
            {
                "provider": target.provider,
                "model": target.model,
                "system": system,
                "parts": raw_parts,
                "schema": schema_json,
                "sampling_params": sampling,
            }
        )
        request_json = _canonical(
            {
                "provider": target.provider,
                "model": target.model,
                "system": _scrub_secrets(system) or "",
                "parts": [
                    [_scrub_secrets(lbl) or "", _scrub_secrets(txt) or ""] for lbl, txt in raw_parts
                ],
                "schema": schema_json,
                "sampling_params": sampling,
                "mode": mode,
                "user_message": _scrub_secrets(user_message) or "",
            }
        )
        est_input_tokens = estimate_tokens(system) + estimate_tokens(user_message)
        if schema_json is not None:
            est_input_tokens += estimate_tokens(_canonical(schema_json))
        call = _Call(
            target=target,
            effective=effective,
            system=system,
            parts=norm_parts,
            user_message=user_message,
            schema_json=schema_json,
            sampling=sampling,
            subject_key=self.subject_key_getter(),
            cache_key=cache_key,
            request_json=request_json,
            request_hash=_sha256(request_json),
            prompt_hash=_sha256({"system": system, "parts": raw_parts}),
            est_input_tokens=est_input_tokens,
        )

        context_limit = int(effective["context_tokens"])
        reserve = resolve_reserve_tokens(context_limit, effective)
        budget_tokens = max(1, context_limit - reserve)
        if est_input_tokens > budget_tokens:
            err_msg = (
                f"input_too_large: estimated {est_input_tokens} tokens exceeds "
                f"available context budget {budget_tokens} "
                f"(context={context_limit}, reserve={reserve})"
            )
            self._record_call(
                call,
                input_tokens=est_input_tokens,
                output_tokens=None,
                cost_usd=None,
                latency_ms=0,
                cached=False,
                repairs_count=0,
                status="refused",
                error=err_msg,
            )
            raise InputTooLarge(err_msg, input_tokens=est_input_tokens, limit_tokens=budget_tokens)
        return call

    def _fetch_or_call(self, call: _Call) -> _Response:
        target = call.target
        with _key_lock(call.cache_key):
            if not self.no_cache:
                with _CONN_LOCK:
                    row = self.conn.execute(
                        "SELECT raw_response, input_tokens, output_tokens, cost_usd "
                        "FROM llm_cache WHERE cache_key = ?",
                        (call.cache_key,),
                    ).fetchone()
                if row is not None:
                    raw_resp = row["raw_response"]
                    return _Response(
                        raw_text=raw_resp,
                        input_tokens=(
                            row["input_tokens"]
                            if row["input_tokens"] is not None
                            else call.est_input_tokens
                        ),
                        output_tokens=(
                            row["output_tokens"]
                            if row["output_tokens"] is not None
                            else estimate_tokens(raw_resp)
                        ),
                        cost_usd=row["cost_usd"],
                        latency_ms=0,
                        cached=True,
                    )

            req = TransportRequest(
                provider=target.provider,
                model=target.model,
                base_url=target.base_url,
                system=call.system,
                parts=call.parts,
                user_message=call.user_message,
                schema=call.schema_json,
                sampling_params={
                    k: call.sampling[k] for k in SAMPLING_PARAMS if k in call.sampling
                },
                timeout_s=float(call.effective.get("timeout_s", 60.0)),
            )

            transport = self._active_transport()
            self._ensure_server(target, transport)

            # Subject key only — never the prompt — so a long local-model call is visible.
            if self.on_before_call is not None:
                self.on_before_call(call.subject_key)

            retry_budget_s = float(call.effective.get("retry_budget_s", DEFAULT_RETRY_BUDGET_S))
            start_t = time.monotonic()
            # Measure retry budget from the first transient failure rather than before
            # attempt 1, so a request whose `timeout_s` exceeds `retry_budget_s` still
            # gets its configured retry window once attempt 1 fails.
            retry_start_t: float | None = None
            attempt = 0
            backoff = 0.05

            while True:
                attempt += 1
                call_t0 = time.monotonic()
                try:
                    resp = transport.request(req)
                    latency_ms = max(0, int((time.monotonic() - call_t0) * 1000))
                    break
                except Exception as exc:
                    err = (
                        exc
                        if isinstance(exc, LLMTransportError)
                        else _classify_transport_exception(exc)
                    )
                    now = time.monotonic()
                    if retry_start_t is None:
                        retry_start_t = now
                    remaining = retry_budget_s - (now - retry_start_t)
                    if err.transient and remaining > 0:
                        sleep_s = min(backoff, remaining)
                        if sleep_s > 0:
                            time.sleep(sleep_s)
                        if (time.monotonic() - retry_start_t) < retry_budget_s:
                            backoff = min(backoff * 2.0, 2.0)
                            continue
                    latency_ms = max(0, int((time.monotonic() - start_t) * 1000))
                    call_id = self._record_call(
                        call,
                        input_tokens=call.est_input_tokens,
                        output_tokens=None,
                        cost_usd=None,
                        latency_ms=latency_ms,
                        cached=False,
                        repairs_count=0,
                        status="transport_error",
                        error=str(err),
                    )
                    target_desc = f"{target.provider}:{target.model}"
                    if target.base_url:
                        target_desc = f"{target_desc} @ {target.base_url}"
                    ctx_parts = [
                        f"subject={call.subject_key}" if call.subject_key else None,
                        f"target={target_desc}",
                        f"attempts={attempt}",
                        f"elapsed={latency_ms / 1000:.1f}s",
                        f"timeout_s={req.timeout_s}",
                        f"est_input_tokens={call.est_input_tokens}",
                        f"call_id={call_id}",
                    ]
                    ctx_suffix = ", ".join(p for p in ctx_parts if p is not None)
                    enriched_msg = (
                        _scrub_secrets(f"{err} [{ctx_suffix}]") or f"{err} [{ctx_suffix}]"
                    )
                    raise LLMTransportError(
                        enriched_msg,
                        status_code=err.status_code,
                        transient=err.transient,
                        call_id=call_id,
                        attempts=attempt,
                        subject_key=call.subject_key,
                    ) from exc

            raw_text = _scrub_secrets(resp.raw_text) or ""
            in_tokens = (
                resp.input_tokens if resp.input_tokens is not None else call.est_input_tokens
            )
            out_tokens = (
                resp.output_tokens if resp.output_tokens is not None else estimate_tokens(raw_text)
            )

            # Write to llm_cache BEFORE validation so editing a validator never re-calls the model.
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO llm_cache (cache_key, provider, model, request_json, raw_response,"
                    " input_tokens, output_tokens, cost_usd, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (cache_key) DO UPDATE SET"
                    " raw_response = excluded.raw_response,"
                    " input_tokens = excluded.input_tokens,"
                    " output_tokens = excluded.output_tokens,"
                    " cost_usd = excluded.cost_usd,"
                    " created_at = excluded.created_at",
                    (
                        call.cache_key,
                        target.provider,
                        target.model,
                        call.request_json,
                        raw_text,
                        in_tokens,
                        out_tokens,
                        resp.cost_usd,
                        utc_now(),
                    ),
                )

            return _Response(
                raw_text=raw_text,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cost_usd=resp.cost_usd,
                latency_ms=latency_ms,
                cached=False,
            )

    def _on_validation_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def _on_validation_failure(self, effective: Mapping[str, Any], exc: ExtractionFailed) -> None:
        max_failures = int(
            effective.get("max_consecutive_failures", DEFAULT_MAX_CONSECUTIVE_FAILURES)
        )
        with self._lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if max_failures > 0 and count >= max_failures and self.breaker_error is None:
                self.breaker_error = SystematicModelFailure(
                    f"model_systematic_failure: {count} consecutive ExtractionFailed outcomes "
                    f"(last: {exc})"
                )

    def _record_response(
        self,
        call: _Call,
        resp: _Response,
        *,
        status: str,
        repairs_count: int,
        error: str | None = None,
    ) -> int:
        return self._record_call(
            call,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cost_usd=resp.cost_usd,
            latency_ms=resp.latency_ms,
            cached=resp.cached,
            repairs_count=repairs_count,
            status=status,
            error=error,
        )

    def _record_call(
        self,
        call: _Call,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
        latency_ms: int,
        cached: bool,
        repairs_count: int,
        status: str,
        error: str | None,
    ) -> int:
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO llm_calls (run_id, stage, subject_key, provider, model, cache_key,"
                " request_hash, prompt_hash, input_tokens, output_tokens, cost_usd, latency_ms,"
                " cached, repairs_count, status, error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.run_id,
                    self.stage,
                    call.subject_key,
                    call.target.provider,
                    call.target.model,
                    call.cache_key,
                    call.request_hash,
                    call.prompt_hash,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    latency_ms,
                    1 if cached else 0,
                    repairs_count,
                    status,
                    _scrub_secrets(error),
                    utc_now(),
                ),
            )
            return int(cur.lastrowid)
