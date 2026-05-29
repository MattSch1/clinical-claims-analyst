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

## Results (measured, synthetic data, `gpt-4o-mini`)

| Metric | Value |
|---|---|
| **Result-set accuracy** (vs gold SQL, 20 cases) | **~60–65%** (`m5-v1`, up from ~40–50% `m2-v1`: **+15–20 pts**) |
| **Self-correction recovery** | up to **3/3** first-attempt errors rescued |
| **LLM-judge faithfulness** | mean **~4.1/5**, validated at **Cohen's κ = 1.0** vs human labels (target ≥ 0.70) |
| **PHI leakage** | **0**, enforced in code + CI |
| **Cost / query** | **~$0.0004** · **p95 latency** ~6–10 s |

**The PHI-safe story (the scarce signal):** a de-identified-views boundary, a
least-privilege DB role that **physically cannot read base tables or the date-shift key**,
an **append-only access audit** (separate INSERT-only writer + no-mutate trigger), a
**no-PHI-to-model** regime (input/output scanned; row-level `SELECT *` and raw surrogate
keys refused), and an automated **leakage scanner that asserts zero** — all demonstrated
on synthetic data, all real code. See *Methodology & safety architecture* below.

---

## Status: Milestone M6 (shipped: UI · CI gate · writeup) ✅

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

- **M5 — optimize + measure:** one lever — **prompt revision** (`m5-v1`): prefer
  `description` over `code` for top-N questions, JOIN `v_payers` for payer names, honor
  the requested rate unit, and `ILIKE` on description when a code is unknown.
  **Result-set accuracy ~40–50% (`m2-v1`) → ~60–65% (`m5-v1`)** — a **+15–20 point** gain
  at equal cost (~$0.0004/query), lower p95 latency (~15 s → ~6–10 s), recovery 3/3, and
  PHI leakage still 0. Remaining misses (rate phrasing, per-patient subqueries, window
  functions, exact code-based cohorts) are documented levers for model-routing / few-shot.

- **M6 — ship:** a minimal **demo UI** at `GET /` (question → `/ask`, renders answer, SQL,
  cost, audit id, trace link), a **CI quality + PHI gate** (`.github/workflows/eval.yml`:
  every PR runs lint + the PHI-control/leakage/guard tests; an opt-in `full-eval` job spins
  a Postgres service, bootstraps the views, and fails on accuracy regression *or* any
  leakage), a deploy blueprint (`render.yaml`), and this metrics-first writeup.

The PHI controls are layered defense-in-depth — the **DB grant is the primary control**
(the agent's role physically cannot read a base table), with code-level guards
(`assert_views_only`, `assert_aggregate_shape`, `assert_safe_output_columns`) as a fast,
structured second line.

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

## Methodology & safety architecture

**Eval methodology (why the numbers are credible).** The primary scorer is a
*deterministic result-set match*: the runner executes both the gold SQL and the agent's
SQL against the de-identified views and compares the result sets order- and
precision-normalized — the agent **never sees `gold_sql`**, only the view schema and the
question. Alongside it: execution-success, self-correction recovery, and a PHI-leakage
count. The secondary scorer is an **LLM judge** of answer faithfulness — and because a
judge is itself a model output, it is **validated against human labels** (Cohen's κ on
the binary faithful decision; κ = 1.0 here, target ≥ 0.70). A useful, honest finding:
faithfulness and correctness **decouple** — the agent often *faithfully* reports a
*wrong-SQL* result — which is exactly why result-set match is the primary metric and
where optimization (M5) was aimed.

**PHI-safe architecture (the scarce signal), in layers:**
1. **Classification** — every base column tagged IDENTIFIER / QUASI / ANALYTIC against the
   HIPAA Safe Harbor 18 identifiers (ages > 89 bucketed to 90+).
2. **De-identified views** — `v_*` views drop identifiers and transform quasi-identifiers
   (age → band, dates → per-patient *shifted* year, ZIP → 3-digit). The shift preserves
   within-patient intervals (so readmission/LOS still work) without absolute dates.
3. **Least-privilege role** — the agent connects as `analyst_ro`: `SELECT` on the views
   *only*, `default_transaction_read_only`, a statement timeout, and an explicit `REVOKE`
   on base tables **and on `patient_date_offset`** (reading the offset would let an
   attacker reverse the shift — the key re-identification vector). This is the **primary**
   control; the agent physically cannot reach PHI.
4. **No-PHI-to-model** — `schema_inspect` exposes only the view schema; code guards reject
   non-SELECT, base-table/system-catalog references, bare `SELECT *`, and results that
   expose a raw surrogate key (one row per individual).
5. **De-id boundary + leakage scanner** — every model input and output crosses a scrubber
   and a high-precision identifier scanner that **asserts zero leakage** (a hard failure),
   enforced in tests + CI.
6. **Append-only audit** — every execution writes one immutable row (request id, SQL, views,
   row count, outcome) via a *separate INSERT-only* role, with a no-UPDATE/DELETE trigger.

## Deploy

The app is a stateless container; production needs a **managed Postgres** (bootstrap the
views into it once), **Langfuse** (self-hosted or [Cloud](https://cloud.langfuse.com)),
and an `OPENAI_API_KEY`. A Render blueprint is in [`render.yaml`](./render.yaml); the
container honors `$PORT`.

```bash
# 1. Provision managed Postgres + set the DSNs/role passwords + Langfuse + OPENAI_API_KEY
#    as service env vars (see render.yaml / .env.example). Then, once, against that DB:
DATA_BACKEND=postgres python -m src.data.bootstrap_pg   # creates views, analyst_ro, audit
# 2. Deploy the Docker service (Render/Fly/any container host). Health check: GET /health.
# 3. Open the service URL → the demo UI at GET /.
```

The reachable URL is the operator's step (it needs your cloud account + secrets); the repo
is deploy-ready and runs end-to-end locally via `docker compose up -d --build`.

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
