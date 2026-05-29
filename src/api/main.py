"""FastAPI surface for the Clinical & Claims Analyst Agent.

M1: GET /health and POST /ask, where /ask runs the LangGraph agent
(plan → generate_sql → execute_sql → validate → self_correct → synthesize) over the
SQLite population and returns a cited answer plus the SQL, row count, cost, and a
Langfuse trace URL. JSON in / JSON out; no HTML <form> coupling (spec §11).

The whole request is one Langfuse trace; each node's model call and the SQL tool call
are nested observations under it.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from src.agent.graph import run_agent
from src.config import get_settings
from src.obs import tracing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the Langfuse client (logs whether tracing is enabled).
    tracing.get_client()
    yield
    # Flush and stop background threads cleanly.
    tracing.shutdown()


app = FastAPI(
    title="Clinical & Claims Analyst Agent",
    version="0.6.0",
    description=(
        "PHI-safe text-to-analytics over a synthetic claims+clinical dataset. The agent "
        "plans, writes SQL against de-identified views, self-corrects, and answers — "
        "audited, traced, leakage-scanned. JSON in/out; demo UI at /."
    ),
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=2000,
        examples=["What is the average total claim cost per encounter by encounter class?"],
    )


class AskResponse(BaseModel):
    answer: str
    sql: str | None = None
    row_count: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    model: str
    trace_url: str | None = None
    audit_id: int | None = None      # access_audit row id (the audit trail in action)
    leakage_count: int = 0           # PHI-leakage findings on model I/O (target: 0)
    note: str | None = None


_UI_INDEX = Path(__file__).resolve().parents[2] / "ui" / "index.html"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> object:
    """Minimal demo UI (JSON API is the source of truth; this is just a client)."""
    if _UI_INDEX.exists():
        return FileResponse(_UI_INDEX)
    return HTMLResponse(
        "<h1>Clinical &amp; Claims Analyst</h1>"
        "<p>Demo UI not found. POST JSON to <code>/ask</code> or see <code>/docs</code>.</p>"
    )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": "0.6.0",
        "milestone": "M6",
        "data_backend": settings.data_backend,
        "llm_configured": settings.llm_ready,
        "langfuse_configured": settings.langfuse_ready,
        "postgres_configured": settings.pg_ready,
    }


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    # Without a model (and not mocking), don't pretend — return a clear message.
    if not settings.llm_ready and not settings.llm_mock:
        return AskResponse(
            answer="No language model is configured, so I can't analyze the question yet.",
            model=settings.llm_model,
            note="Set OPENAI_API_KEY (or LLM_MOCK=true) to enable the agent.",
        )

    t0 = time.perf_counter()
    request_id = uuid4().hex
    state = None
    trace_link = None
    try:
        # One trace per request; node generations + the SQL tool call nest under it.
        with tracing.span(
            name="ask",
            input=req.question,
            trace_name="ask",
            tags=["m2", "ask", "agent"],
            metadata={"milestone": "M2", "request_id": request_id},
        ) as root:
            state = run_agent(req.question, request_id=request_id)
            root["output"] = state.final_answer
        trace_link = tracing.trace_url(root.get("trace_id"))
    except Exception as exc:  # never 500 — graceful degradation (hard rule)
        logger.exception("/ask agent run failed")
        return AskResponse(
            answer="I couldn't determine an answer due to an internal error.",
            model=settings.llm_model,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            note=f"agent error: {exc}",
        )
    finally:
        tracing.flush()  # deliver the trace before responding (async/buffered)

    note = (
        f"status={state.status}; self_corrections={state.retry_count}; "
        f"tokens={state.total_tokens}"
    )
    return AskResponse(
        answer=state.final_answer or "I couldn't determine an answer from the available data.",
        sql=state.candidate_sql,
        row_count=state.row_count,
        latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        cost_usd=round(state.total_cost_usd, 6),
        model=settings.llm_model,
        trace_url=trace_link,
        audit_id=state.audit_id,
        leakage_count=len(state.leakage_hits),
        note=note,
    )
