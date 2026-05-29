"""Langfuse (self-hosted) observability wiring.

Design note — why we DON'T use LiteLLM's `langfuse` callback:
The current Langfuse Python SDK is v4 (OpenTelemetry-based). LiteLLM's *native*
`success_callback=["langfuse"]` was written against the langfuse **v2** API and
breaks on v3/v4 (BerriAI/litellm#13137). So instead of that callback, we drive
the Langfuse v4 SDK directly: each model call is wrapped in a *generation* via a
context manager. This gives us (a) deterministic flushing — `langfuse.flush()`
is the officially documented way to guarantee delivery before a short-lived
request/script exits — and (b) legible, hand-instrumented traces, which fits the
project's "the agent logic is visible" goal. LiteLLM still makes the model call
and computes cost; we just record it.

Everything degrades to a safe no-op when Langfuse isn't configured, so the app
never fails because tracing is down (a hard rule: never 500 on an infra issue).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from src.config import get_settings

logger = logging.getLogger(__name__)

_client: Any | None = None
_init_attempted = False


def get_client() -> Any | None:
    """Return the configured Langfuse client, or None if tracing is off/unavailable.

    Initialized lazily and memoized; safe to call from anywhere.
    """
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True

    settings = get_settings()
    if not settings.langfuse_ready:
        logger.info("Langfuse not configured (keys missing or disabled); tracing is a no-op.")
        return None

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:  # pragma: no cover - import/config failure path
        logger.warning("Langfuse client init failed; continuing without tracing.", exc_info=True)
        _client = None
    return _client


def is_enabled() -> bool:
    return get_client() is not None


def verify_auth() -> bool:
    """Best-effort credential check against the Langfuse server (used by smoke test)."""
    client = get_client()
    if client is None:
        return False
    try:
        return bool(client.auth_check())
    except Exception:
        logger.warning("Langfuse auth_check failed.", exc_info=True)
        return False


def flush() -> None:
    """Block until queued spans are delivered. Call before a request/script returns."""
    client = get_client()
    if client is not None:
        _safe(client.flush)


def shutdown() -> None:
    """Flush and stop background threads. Call on app shutdown, not per-request."""
    if _client is not None:
        _safe(_client.shutdown)


def trace_url(trace_id: str | None) -> str | None:
    if not trace_id:
        return None
    settings = get_settings()
    return (
        f"{settings.langfuse_browser_url}/project/"
        f"{settings.langfuse_project_id}/traces/{trace_id}"
    )


def _safe(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Run a Langfuse SDK call, swallowing errors (tracing must never break a request)."""
    try:
        return fn(**kwargs) if kwargs else fn()
    except Exception:
        logger.warning(
            "Langfuse call %s failed; ignoring.", getattr(fn, "__name__", fn), exc_info=True
        )
        return None


@contextmanager
def _observation(
    *,
    as_type: str,
    name: str,
    input: Any = None,
    model: str | None = None,
    trace_name: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Open a langfuse v4 observation (span / generation / tool) as the *current*
    context, so observations opened inside this block nest as children of the same
    trace (OTEL context propagation).

    Yields a mutable ``box``; before exiting, the caller may set box["output"],
    box["usage_details"] = {input, output, total}, box["cost_details"] = {total};
    and can read box["trace_id"]. Degrades to a no-op (body still runs) when tracing
    is unavailable — a request must never fail because tracing is down.
    """
    box: dict[str, Any] = {"trace_id": None}
    client = get_client()

    # Fold trace-level hints into metadata. langfuse v4 derives the trace name from
    # the root observation; keeping these in metadata leaves them legible in the UI
    # without the v3-era update_current_trace() (removed in v4).
    md: dict[str, Any] = dict(metadata or {})
    for key, val in (
        ("trace_name", trace_name),
        ("user_id", user_id),
        ("session_id", session_id),
        ("tags", tags),
    ):
        if val is not None:
            md.setdefault(key, val)

    cm = None
    if client is not None:
        kwargs: dict[str, Any] = {"name": name, "as_type": as_type, "input": input, "metadata": md}
        if model is not None:
            kwargs["model"] = model
        cm = _safe(client.start_as_current_observation, **kwargs)

    if cm is None:
        yield box  # tracing unavailable — run the body untraced
        return

    with cm as obs:
        box["trace_id"] = _safe(client.get_current_trace_id)
        yield box  # caller runs the work and fills the box
        _safe(
            obs.update,
            output=box.get("output"),
            usage_details=box.get("usage_details"),
            cost_details=box.get("cost_details"),
        )


@contextmanager
def generation(
    *,
    name: str,
    model: str,
    input: Any,
    trace_name: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Trace one model call as a generation. Caller fills box['output'],
    box['usage_details'] = {input, output, total}, box['cost_details'] = {total}."""
    with _observation(
        as_type="generation",
        name=name,
        input=input,
        model=model,
        trace_name=trace_name,
        user_id=user_id,
        session_id=session_id,
        tags=tags,
        metadata=metadata,
    ) as box:
        yield box


@contextmanager
def span(
    *,
    name: str,
    input: Any = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Open a span — use as the per-request root (children nest under it)."""
    with _observation(
        as_type="span",
        name=name,
        input=input,
        trace_name=trace_name,
        tags=tags,
        metadata=metadata,
        user_id=user_id,
        session_id=session_id,
    ) as box:
        yield box


@contextmanager
def tool(
    *,
    name: str,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Trace a tool call (e.g. sql_execute)."""
    with _observation(as_type="tool", name=name, input=input, metadata=metadata, tags=tags) as box:
        yield box
