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

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from src.agent.graph import run_agent
from src.api.ratelimit import RateLimiter
from src.config import get_settings
from src.obs import tracing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()
_limiter = RateLimiter(
    per_minute=settings.rate_limit_per_min, per_day_total=settings.max_queries_per_day
)

MAX_BODY_BYTES = 64 * 1024  # /ask takes a short question; reject oversized bodies pre-parse


def _client_ip(request: Request) -> str:
    # Render (and CF-fronted hosts) place the real client IP as the FIRST entry of
    # X-Forwarded-For and control that position, so it isn't client-spoofable; a dedicated
    # CF-Connecting-IP / True-Client-IP header, when the platform sets it, is even better.
    for header in ("cf-connecting-ip", "true-client-ip"):
        val = request.headers.get(header)
        if val:
            return val.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail LOUDLY on an impossible configuration — a misconfigured public deploy must
    # not boot, half-work, and burn tokens answering nothing.
    if settings.use_postgres:
        missing = [
            name
            for name, val in (
                ("ANALYST_RO_DSN", settings.analyst_ro_dsn),
                ("AUDIT_WRITER_DSN", settings.audit_writer_dsn),  # audit is fail-closed
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                f"DATA_BACKEND=postgres but {', '.join(missing)} not set — refusing to start."
            )
    if not settings.llm_ready and not settings.llm_mock:
        logger.warning("No OPENAI_API_KEY and LLM_MOCK is off — /ask will refuse questions.")
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


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        length = request.headers.get("content-length")
        if length is None:
            # No declared length = chunked; we'd have to buffer it to size it. Require a
            # length instead (legit JSON clients always send one) so the cap can't be
            # bypassed by a Transfer-Encoding: chunked body.
            return JSONResponse(status_code=411, content={"detail": "Content-Length required"})
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"detail": "request body too large"})
    return await call_next(request)


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


_db_health: dict = {"checked": 0.0, "ok": True, "detail": "not checked"}
_DB_HEALTH_TTL_S = 30.0


def _database_health() -> tuple[bool, str]:
    """Cheap cached SELECT 1 over the analyst_ro path, so /health (and therefore the
    deploy gate) actually reflects DB reachability instead of env-var presence."""
    if not settings.use_postgres:
        return True, "sqlite"
    now = time.monotonic()
    if now - _db_health["checked"] < _DB_HEALTH_TTL_S:
        return _db_health["ok"], _db_health["detail"]
    from src.data import pg

    try:
        pg.ping()
        ok, detail = True, "ok"
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
        ok, detail = False, f"error: {type(exc).__name__}: {exc}"
    _db_health.update(checked=now, ok=ok, detail=detail)
    return ok, detail


@app.get("/health")
def health() -> object:
    db_ok, db_detail = _database_health()
    body = {
        "status": "ok" if db_ok else "degraded",
        "service": settings.app_name,
        "version": "0.6.0",
        "milestone": "M6",
        "data_backend": settings.data_backend,
        "database": db_detail,
        "llm_configured": settings.llm_ready,
        "langfuse_configured": settings.langfuse_ready,
        "postgres_configured": settings.pg_ready,
    }
    if not db_ok:
        return JSONResponse(status_code=503, content=body)
    return body


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, request: Request, background_tasks: BackgroundTasks) -> AskResponse:
    # Rate limit BEFORE any model call so a public URL can't drain the key (429).
    if _limiter.enabled:
        allowed, reason = _limiter.check(_client_ip(request), now=time.time())
        if not allowed:
            raise HTTPException(status_code=429, detail=reason)

    # Deliver the trace AFTER the response is sent — flush() blocks for the exporter
    # timeout when Langfuse is unreachable, and the client shouldn't pay for that.
    background_tasks.add_task(tracing.flush)

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
