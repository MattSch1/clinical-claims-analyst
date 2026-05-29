"""M0 smoke test: make ONE real, traced LiteLLM call and CONFIRM it landed in
Langfuse via the public API. This is the "show me the trace working" check.

Run from the repo root with the Langfuse stack up and OPENAI_API_KEY set:

    python scripts/smoke_trace.py

Exit codes: 0 confirmed · 2 missing config · 3 langfuse auth · 4 no trace id · 5 not found.
"""

from __future__ import annotations

import sys
import time

import httpx

from src.config import get_settings
from src.llm import client as llm
from src.obs import tracing


def main() -> int:
    s = get_settings()
    print(
        f"[smoke] model={s.llm_model} llm_ready={s.llm_ready} "
        f"langfuse_ready={s.langfuse_ready} host={s.langfuse_host}"
    )
    if not s.llm_ready and not s.llm_mock:
        print(
            "[smoke] no model key set and LLM_MOCK is off — cannot make a call.",
            file=sys.stderr,
        )
        return 2
    if not s.langfuse_ready:
        print("[smoke] Langfuse keys not set — cannot verify a trace.", file=sys.stderr)
        return 2
    if not tracing.verify_auth():
        print(
            "[smoke] Langfuse auth_check failed — is the stack up / keys correct?",
            file=sys.stderr,
        )
        return 3

    msg = [{"role": "user", "content": "Say 'pong' and nothing else."}]
    result = None
    trace_id = None
    with tracing.generation(
        name="smoke_generation",
        model=s.llm_model,
        input="ping",
        trace_name="m0_smoke",
        tags=["m0", "smoke"],
        metadata={"milestone": "M0"},
    ) as box:
        try:
            result = llm.complete(msg)
        except Exception as exc:  # noqa: BLE001
            blob = f"{type(exc).__name__}: {exc}".lower()
            quota_keys = ("ratelimit", "insufficient_quota", "quota", "authentication")
            if any(k in blob for k in quota_keys):
                print(
                    f"[smoke] live provider call failed ({type(exc).__name__}); falling back to "
                    "LiteLLM mock_response to prove the trace pipeline."
                )
                result = llm.complete(msg, mock_response="pong (mocked: live provider unavailable)")
            else:
                raise
        box["output"] = result.text
        box["usage_details"] = {
            "input": result.prompt_tokens,
            "output": result.completion_tokens,
            "total": result.total_tokens,
        }
        box["cost_details"] = {"total": result.cost_usd}
        trace_id = box.get("trace_id")
    tracing.flush()

    print(
        f"[smoke] model replied {result.text!r} | mocked={result.mocked} "
        f"tokens={result.total_tokens} cost=${result.cost_usd:.6f} "
        f"latency={result.latency_ms:.0f}ms"
    )
    if not trace_id:
        print("[smoke] no trace_id captured (tracing disabled?).", file=sys.stderr)
        return 4
    print(f"[smoke] trace_id={trace_id}")
    print(f"[smoke] trace_url={tracing.trace_url(trace_id)}")

    # Ingestion is async — poll the public API until the trace is queryable.
    api = f"{s.langfuse_host.rstrip('/')}/api/public/traces/{trace_id}"
    auth = (s.langfuse_public_key or "", s.langfuse_secret_key or "")
    for attempt in range(1, 16):
        try:
            r = httpx.get(api, auth=auth, timeout=10.0)
            if r.status_code == 200:
                print(f"[smoke] CONFIRMED: trace present in Langfuse (after {attempt} attempt(s)).")
                return 0
            if r.status_code not in (404, 400):
                print(f"[smoke]   attempt {attempt}: HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"[smoke]   attempt {attempt}: {exc}")
        time.sleep(2)

    print("[smoke] trace not found via API within timeout.", file=sys.stderr)
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
