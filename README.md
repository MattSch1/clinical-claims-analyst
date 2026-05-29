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

## Status: Milestone M4 (validated LLM judge) ✅

- **M0 — skeleton:** repo scaffold (§7), pinned deps, Docker Compose (self-hosted
  Langfuse v3 stack + Postgres + app), Synthea→SQLite loader, FastAPI `/health` +
  `/ask`, LiteLLM→one model, Langfuse tracing (verified via the API).
- **M1 — agent loop:** LangGraph graph with six nodes (plan → generate_sql →
  execute_sql → validate → self_correct → synthesize), read-only SELECT-only guard,
  schema introspection with categorical-value grounding, self-correction cap 3 +
  graceful degradation, one nested Langfuse trace per request.
- **M2 — PHI-safe layer:** Synthea loaded into **Postgres**; the de-identified
  **`v_*` views** (`src/data/views.sql`, per-patient date-shift, age-banding, ZIP
  truncation) applied via an idempotent bootstrap; a **least-privilege `analyst_ro`
  role** that can `SELECT` the views *only* (never base tables or `patient_date_offset`);
  an **append-only access audit** written by a separate INSERT-only `audit_writer`
  role + a no-UPDATE/DELETE trigger; a **PHI-leakage scanner** on every model
  input/output; and the agent **switched off SQLite base tables onto the views**.

  A post-M2 security review added a **semantic output-column guard** (rejects a raw
  surrogate key like `patient` in results — `SELECT DISTINCT patient` would otherwise
  return one row per individual), an **information_schema/pg_catalog block**, and a
  **scan of every model input** (not just the question).
- **M3 — eval harness:** `python eval/run_eval.py` runs the agent over the 20 labeled
  cases (the agent never sees `gold_sql`), scores **result-set match** (order- and
  precision-normalized) vs the executed gold SQL, and writes a timestamped report with
  accuracy by difficulty/tag, recovery rate, p50/p95 latency, cost/query, and PHI
  leakage. **Baseline: ~50% result-set accuracy, 0 PHI leakage** (gpt-4o-mini). The
  misses cluster into clear optimization levers for M5 (code-vs-description grouping,
  ICD-10-vs-SNOMED code grounding, rate/join phrasing).

- **M4 — validated LLM judge:** a rubric-based faithfulness judge (`eval/judge.py`,
  1–5) scores how well each answer reflects its query result, run as the eval's
  secondary scorer (mean ~4.1/5). Crucially, the judge is **validated against human
  labels** (`eval/judge_validation.py`, `eval/judge_labels.jsonl`): **Cohen's
  κ = 1.0** on the binary faithful decision (target ≥ 0.70), 79% exact 1–5 agreement.
  The judge (faithfulness) and result-set match (correctness) **decouple** — the agent
  often *faithfully* reports a *wrong-SQL* result — which is precisely the M5 target.

The PHI controls are layered defense-in-depth — the **DB grant is the primary control**
(the agent's role physically cannot read a base table), with code-level guards
(`assert_views_only`, `assert_aggregate_shape`, `assert_safe_output_columns`) as a fast,
structured second line.

Not yet built (deliberately): optimization (M5) and the CI gate + deploy (M6).

### Verified end-to-end (M2)

- `analyst_ro` → `SELECT FROM v_patients` ✅; `FROM patients` / `FROM patient_date_offset`
  → **permission denied** (DB-level).
- The agent answers 10/10 demo questions querying **only `v_*` views**; every execution
  writes one **audit row** (ok / rejected / error); an `UPDATE` on the audit table is
  blocked by the trigger.
- A bare `SELECT *` is **refused** (and audited as `rejected`).
- A question containing an SSN is **refused before the model** (`status=refused`); the
  leakage scanner reports **0** hits on aggregate output.

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

### 4b. (M2) Bring up Postgres + the de-identified views

```bash
docker compose up -d analytics-postgres
# In .env set DATA_BACKEND=postgres and the three DSNs + role passwords (see .env.example).
python -m src.data.bootstrap_pg   # loads base tables, applies views.sql, creates
                                  # analyst_ro (views-only) + audit_writer (INSERT-only)
```

This is idempotent (re-runnable). After it, the agent queries the de-identified `v_*`
views via the least-privilege role; every execution is audited; `SELECT *` and any
base-table reference are refused.

### 5. Run the API

```bash
uvicorn src.api.main:app --reload
curl localhost:8000/health     # shows milestone, data_backend, postgres_configured
curl -s localhost:8000/ask -H 'content-type: application/json' \
     -d '{"question":"Average total claim cost per encounter by class?"}' | python -m json.tool
# the response includes sql, row_count, cost_usd, trace_url, and audit_id
python scripts/m1_demo.py       # 10-question demo over the de-identified views
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
