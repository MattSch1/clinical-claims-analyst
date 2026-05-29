# Clinical & Claims Analyst Agent

An autonomous **text-to-analytics agent** over a **synthetic** claims + clinical
dataset ([Synthea](https://github.com/synthetichealth/synthea)). It plans,
generates SQL against **de-identified views**, executes it, recovers from its own
errors, and synthesizes a cited, **PHI-safe** answer.

The differentiators (in priority order): a **PHI-safe architecture** (de-id
boundary, access audit, no-PHI-to-model, least-privilege DB role, automated
leakage scanner), a domain-aware **eval harness**, full **observability**
(Langfuse), and **CI quality gates**.

> **Data is 100% synthetic (Synthea). No real PHI is ever used.** The system is
> *architected as if* it handled PHI on purpose — that discipline is the point —
> and the repo is safe to make public.

---

## Status: Milestone M0 (skeleton) ✅

What's wired right now:

- **Repo scaffold** per spec §7, dependencies pinned in `pyproject.toml`.
- **Docker Compose** stack: self-hosted **Langfuse** server (the v3.x platform:
  web · worker · postgres · clickhouse · redis · minio) + an **analytics Postgres**
  (for M2) + the **app**. *(The Langfuse **server** is on the 3.x line; the Langfuse
  **Python SDK** is independently on the 4.x line — two separate version lines that
  work together, verified here end-to-end.)*
- **Synthea → SQLite loader** (`src/data/load.py`) for a tiny (~200 patient)
  population. *(Postgres + de-identified views come in M2.)*
- **FastAPI**: `GET /health` and a stub `POST /ask`.
- **LiteLLM** wired to one model (OpenAI via `OPENAI_API_KEY`).
- **Langfuse tracing**: every model call is recorded as a trace, confirmed by a
  smoke test that queries the Langfuse API.

Not yet built (later milestones, deliberately): the LangGraph agent loop (M1),
the PHI layer + Postgres views (M2), the eval harness (M3+). Those modules exist
as honest placeholders noting their milestone.

### A note on the Langfuse↔LiteLLM integration

Two independent version lines: the self-hosted Langfuse **server** is on **3.x**;
the Langfuse **Python SDK** is on **4.x** (OpenTelemetry-based). They interoperate
(the 4.x SDK posts to the 3.x server's OTEL/public API) — verified end-to-end by
`scripts/smoke_trace.py`. LiteLLM's *native* `langfuse` callback only supports the
old **v2** SDK, so this project instruments the **v4 SDK directly**
(`src/obs/tracing.py`) — wrapping each LiteLLM call in a generation, which gives
deterministic `flush()` delivery and legible traces. (The v4 API uses
`start_as_current_observation(as_type="generation", …)`; see the design note atop
`src/obs/tracing.py`.)

### Verified

`scripts/smoke_trace.py` made a call through LiteLLM and **confirmed the trace in
Langfuse via its public API**. Note: a *live* model call needs a funded
`OPENAI_API_KEY`; if the key has no quota (or `LLM_MOCK=true`), the call falls back
to LiteLLM's `mock_response` so the trace pipeline is still exercised — the trace,
cost, and latency are recorded exactly the same way.

---

## Quick start (local)

Prereqs: Docker + Docker Compose, Python 3.12+ (3.13 works), Java 17+ (only for
Synthea generation), and an `OPENAI_API_KEY`.

### 1. Configure secrets

```bash
cp .env.example .env
# Generate the Langfuse crypto secrets and paste them into .env:
echo "ENCRYPTION_KEY=$(openssl rand -hex 32)"     # exactly 64 hex chars
echo "SALT=$(openssl rand -base64 32)"
echo "NEXTAUTH_SECRET=$(openssl rand -base64 32)"
# Pick fixed Langfuse project keys (must keep the pk-lf- / sk-lf- prefixes) and
# set BOTH the LANGFUSE_INIT_PROJECT_* and the matching LANGFUSE_PUBLIC/SECRET_KEY.
# Ensure OPENAI_API_KEY is set in your shell (or in .env).
```

### 2. Bring up Langfuse (self-hosted)

```bash
docker compose up -d langfuse-web langfuse-worker postgres clickhouse redis minio
# First boot pulls images + runs migrations; wait until the UI is healthy:
curl -fsS http://localhost:3000/api/public/health && echo " langfuse up"
```

Langfuse UI: <http://localhost:3000> (log in with `LANGFUSE_INIT_USER_EMAIL` /
`LANGFUSE_INIT_USER_PASSWORD`). The org/project/API-keys are **seeded headlessly**
from `LANGFUSE_INIT_*`, so no click-through setup is needed.

### 3. Install the app + load a tiny Synthea population

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m src.data.load --patients 200 --seed 12345   # generates + loads SQLite
```

### 4. Confirm the model call → Langfuse trace

```bash
python scripts/smoke_trace.py
# -> prints the model reply, the trace_url, and "CONFIRMED: trace present in Langfuse"
```

### 5. Run the API

```bash
uvicorn src.api.main:app --reload
curl localhost:8000/health
curl -s localhost:8000/ask -H 'content-type: application/json' \
     -d '{"question":"What is this service?"}' | python -m json.tool
# the response includes a trace_url you can open in the Langfuse UI
```

### Run everything in containers instead

```bash
docker compose up -d --build         # brings up the app too (on :8000)
```

### Tests & lint

```bash
pytest          # hermetic unit/smoke tests (no network, no LLM key)
ruff check .
```

---

## Layout (spec §7)

```
src/
  agent/      LangGraph nodes + graph (M1)
  phi/        classification.py (here) · scrub/audit/leakage (M2)
  llm/        LiteLLM client wrapper
  obs/        Langfuse tracing helpers
  api/        FastAPI app
  data/       Synthea loader (M0: SQLite) · views.sql (M2)
eval/         dataset.jsonl (here) · scorers/runner/judge (M3+)
tests/        smoke tests now; PHI-control + graph tests with their milestones
scripts/      smoke_trace.py (trace verification)
```

Full engineering spec: `AGENT_PROJECT_SPEC_HEALTHCARE.md`.
Agent guardrails for Claude Code: `CLAUDE.md`.

## Tip: don't want to self-host Langfuse?

The 6-container Langfuse stack is the "infra ownership" choice from the spec. To
trade it for zero local infra, point `LANGFUSE_HOST`/keys at
[Langfuse Cloud](https://cloud.langfuse.com) (free tier) and skip the Langfuse
services in `docker compose up`.
