"""Thin LiteLLM wrapper — the single chokepoint for model calls.

One entrypoint, `complete()`, returns the text plus token usage, cost, and
latency so observability (Langfuse) and eval (cost/query) get real numbers.
Provider-agnostic by design; M0 wires OpenAI via OPENAI_API_KEY, but the model
string is configurable (LLM_MODEL) so a second/cheaper model can be added later.

Tracing is NOT configured here — `src.obs.tracing.init_tracing()` registers the
Langfuse callback once at startup; this module just forwards `metadata` so each
call is attributable in a trace.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import litellm

from src.config import get_settings

# Be forgiving about provider-specific params (e.g. temperature on models that
# don't accept it) instead of hard-failing a request.
litellm.drop_params = True


# Used when mock mode is on (LLM_MOCK) or no live provider is reachable. The call
# still flows through LiteLLM's full pipeline, so the trace/cost path is exercised.
MOCK_RESPONSE = "pong (LiteLLM mock_response — no live provider call was made)"


@dataclass(slots=True)
class LLMResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: float
    mocked: bool = False


def complete(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    metadata: dict[str, Any] | None = None,
    mock_response: str | None = None,
) -> LLMResult:
    """Run one chat completion through LiteLLM and capture cost/latency/usage.

    If ``mock_response`` is given (or LLM_MOCK is set), LiteLLM returns a canned
    response without calling the provider — the request still traverses the full
    LiteLLM pipeline, so observability/cost wiring is exercised end-to-end. This
    is what lets the trace pipeline be verified without a billable API call.
    """
    settings = get_settings()
    model = model or settings.llm_model
    temperature = settings.llm_temperature if temperature is None else temperature
    max_tokens = max_tokens or settings.llm_max_tokens

    mock = mock_response if mock_response is not None else (
        MOCK_RESPONSE if settings.llm_mock else None
    )

    call_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "metadata": metadata or {},
        # A hung provider call must not pin a worker indefinitely (LiteLLM raises
        # litellm.Timeout, which the agent nodes degrade gracefully on).
        "timeout": settings.llm_timeout,
    }
    if mock is not None:
        call_kwargs["mock_response"] = mock

    t0 = time.perf_counter()
    resp = litellm.completion(**call_kwargs)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    finish_reason = getattr(resp.choices[0], "finish_reason", None)
    if finish_reason == "length":
        # A truncated completion is NOT a usable answer (half a SQL statement parses as
        # garbage); fail loudly so the caller's error path / self-correction handles it.
        raise RuntimeError(
            f"LLM response truncated at max_tokens={max_tokens} (finish_reason=length); "
            "raise LLM_MAX_TOKENS if this recurs"
        )

    text = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)

    try:
        cost_usd = float(litellm.completion_cost(completion_response=resp) or 0.0)
    except Exception:
        # Cost tables don't cover every model; never fail a call over pricing.
        cost_usd = 0.0

    return LLMResult(
        text=text,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        mocked=mock is not None,
    )
