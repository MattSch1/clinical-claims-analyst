# Clinical & Claims Analyst Agent — Engineering Spec (Healthcare Edition)

**Purpose:** Build, deploy, and rigorously evaluate an autonomous agent that answers natural-language questions over a **synthetic claims + clinical dataset** by planning, generating SQL, executing it, recovering from its own errors, and synthesizing a cited, **PHI-safe** answer. The agent loop is the easy half. The differentiators that make this a senior healthcare-AI portfolio artifact are: (1) a rigorous, domain-aware **eval harness**, (2) full **observability**, (3) **CI quality gates**, and (4) a **PHI-safe architecture** demonstrated on synthetic data.

This is the healthcare retargeting of the generic "Agentic Analyst" spec. Same eval/observability/CI scaffolding; healthcare dataset, healthcare eval questions, and a PHI-handling layer that is the scarce signal here. Hand this file to Claude Code as the source of truth.

---

## 1. Why this project (healthcare angle)

The scarce, hard-to-copy combination on the market is **domain knowledge + the ability to build the system**. Generic agent-wiring is a commodity; an engineer who understands claims, clinical coding, payer/quality analytics, *and* can ship a measured agent is rare. Two more reasons this is the right build:

- **The eval set is a domain artifact.** Anyone can write the agent loop. Knowing what a *correct* answer to a claims/quality question looks like is what an outsider can't fake — and it's exactly what makes your eval harness credible. Your domain knowledge is the part of this project that's hardest to copy.
- **PHI-safe architecture is a distinctive signal.** Almost no public portfolio project shows a de-identification boundary, an access audit trail, and a no-PHI-to-model regime. You can demonstrate all of it legitimately on synthetic data. This is the bullet that makes a healthcare hiring manager stop scrolling.

**The task fits the safety model naturally:** "ask your data" analytics is overwhelmingly about *aggregates* (cost trends, utilization rates, prescribing patterns, quality measures), not individual records. So a regime where row-level PHI never reaches the model isn't a limitation — it's the correct design, and you should say so explicitly. That design-judgment point is itself interview gold.

---

## 2. Goals / Non-goals

**Goals**
- An autonomous agent with explicit state, tool use, and automatic error recovery over a clinical+claims schema.
- A custom, domain-aware eval harness: labeled clinical/claims questions, deterministic result-set scoring, and an LLM judge **validated against human labels**.
- A **PHI-safe architecture**: data classification, no-PHI-to-model regime, de-identification boundary, immutable access audit log, least-privilege DB role, and an automated **PHI-leakage check** that asserts zero leakage.
- Full tracing + cost/latency observability.
- A CI regression gate that blocks merges degrading accuracy *or* introducing PHI leakage.
- A live, reachable endpoint + minimal demo UI.
- README + writeup documenting decisions, the PHI-safe design, and measured numbers.

**Non-goals**
- Touching any real PHI. Synthetic data only, always.
- A production EHR/payer integration. Single synthetic dataset is fine.
- Fine-tuning. Hosted models via a gateway.
- Beating SOTA text-to-SQL benchmarks. The methodology + safety architecture are the deliverables.
- Multi-agent orchestration. One well-instrumented, safe agent beats an unmeasured swarm.

---

## 3. Tech stack (committed, with rationale)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Role default; typed throughout. |
| Agent orchestration | **LangGraph** | Battle-tested stateful agents; explicit, inspectable control flow; matches job descriptions. ReAct logic hand-written inside nodes so understanding is visible. |
| Model access | **LiteLLM** | Provider-agnostic gateway; multi-provider fallback; per-call cost/token capture. |
| Models | One strong (planning/synthesis) + one cheap/fast (routing/simple steps) | Lets you tune and report a real cost/latency tradeoff. |
| Data store | **Postgres** (Dockerized) | Real clinical+claims schema, real joins, real failure modes. De-identified **views** layered over base tables (see §7). |
| Serving | **FastAPI** | Async, OpenAPI docs; clean JSON API (no HTML `<form>` coupling). |
| Observability | **Langfuse** (self-hosted via Docker) | Open-source, MIT, free, OTel-aligned. Self-hosting = infra signal. Traces every LLM call, tool call, session. |
| Eval harness | **Custom** + pytest runner | Owning the scoring logic is the differentiator skill. |
| PHI controls | **Custom** (classification map, scrubber, audit logger, leakage scanner) | The scarce healthcare signal. All in your repo. |
| CI | **GitHub Actions** | Runs eval + PHI-leakage check on PRs; gates merges. |
| Deploy | **Docker** + Fly.io or Render | Reachable URL matters most. |
| Demo UI | Minimal React or plain HTML hitting the API | Live demo >> repo alone. |

**README decision sentences (these *are* the skill):** "I chose Langfuse over LangSmith for self-hosted data ownership and no per-trace pricing. I designed a no-PHI-to-model regime because population analytics needs aggregates, not row-level records, so restricting the agent to de-identified views and aggregate outputs is both safer and sufficient for the use case."

---

## 4. The dataset: Synthea (synthetic, realistic, both claims + clinical)

**Default: [Synthea](https://github.com/synthetichealth/synthea)** (MITRE's synthetic patient generator). It produces clinically coherent populations with **both clinical and claims** tables in one schema — no credentialing, no PHI, freely shareable.

Generate a CSV population (e.g. 5,000–20,000 patients) and load these core tables into Postgres:
- **Clinical:** `patients`, `encounters`, `conditions`, `procedures`, `medications`, `observations`, `immunizations`, `careplans`.
- **Claims/financial:** `claims`, `claims_transactions`, `payers`, `payer_transitions`.

This single dataset supports cost analytics, utilization, prescribing patterns, payer mix, and quality measures — the full "ask your claims/clinical data" surface.

**Pure-claims alternative:** CMS **DE-SynPUF** (synthetic Medicare claims) if you want a claims-only, payer-flavored schema. Synthea is the recommended default because it gives you clinical + claims together.

**Quick-start:** generate a tiny Synthea population (≈200 patients) and load to SQLite first so Claude Code can get the agent loop working before wiring Postgres + de-identified views.

---

## 5. PHI-safe architecture (the scarce signal — build this for real)

Even though the data is synthetic, **architect as if it were PHI**. This section is what differentiates the project. Implement every control as real code, not a comment.

**5.1 Data classification map (`src/phi/classification.py`).**
Tag every column as one of: `IDENTIFIER` (direct PHI — name, SSN, address, MRN, phone, email, exact DOB), `QUASI_IDENTIFIER` (age, zip, dates of service), or `ANALYTIC` (diagnosis code, cost, encounter class, etc.). Base this on the **HIPAA Safe Harbor 18 identifiers** — reference that explicitly in the README to show you know the standard. Note the Safe Harbor rule that **ages over 89 must be aggregated** into a single 90+ bucket; enforce it.

**5.2 De-identified views (`src/data/views.sql`).**
Create Postgres **views** over the base tables that drop `IDENTIFIER` columns and bucket `QUASI_IDENTIFIER`s (age → bands, exact dates → year/month, zip → 3-digit). The agent's DB role can read **only the views**, never the base tables. This is "minimum necessary" enforced at the database layer.

**5.3 No-PHI-to-model regime.**
- The schema the agent sees (via `schema_inspect`) is the **view schema** — identifier columns simply don't exist to it.
- The agent is constrained to **aggregate/analytic** queries; a query that would return raw individual records (e.g. `SELECT *` with no aggregation and a small row cap) is rejected or down-routed to a "this returns individual records, which is restricted" response. Population analytics is the supported path.
- Synthesis never echoes a row-level identifier; it reports counts, rates, costs, distributions.

**5.4 De-identification boundary (`src/phi/scrub.py`).**
A single code boundary every value crosses before it can reach a prompt or a response. A regex/heuristic scrubber catches anything identifier-shaped (names, SSN/MRN/phone patterns) and redacts it. On synthetic data it should essentially never fire — which is the point: you prove the boundary exists and holds.

**5.5 Immutable access audit log (`src/phi/audit.py`).**
Every `sql_execute` writes an append-only audit record: timestamp, request_id, the SQL, tables/columns touched, row count returned, and outcome. Mirrors HIPAA audit-control expectations. Store in a separate append-only table; never update or delete.

**5.6 Least-privilege DB role.**
A dedicated read-only role with `SELECT` on **views only**, statement timeout, and no DDL/DML. The agent literally cannot reach base PHI columns.

**5.7 Automated PHI-leakage scanner (`src/phi/leakage.py`) — a distinctive metric.**
A check that scans every model **input** and **output** for identifier patterns and asserts **zero** leakage. Run it in eval and in CI. "Zero PHI leakage across N eval runs, enforced in CI" is a resume bullet no tutorial project has.

---

## 6. High-level architecture

```
                ┌─────────────┐
   HTTP  ─────► │  FastAPI    │ ──► request_id, question
                └──────┬──────┘
                       │
                ┌──────▼───────────────────────────────┐
                │  LangGraph Agent (stateful)           │
                │  plan ─► generate_sql ─► execute_sql  │
                │            ▲                │         │
                │            │ on error       ▼         │
                │         self_correct ◄── validate     │
                │                                │       │
                │                             synthesize │
                └───┬───────────────┬───────────┬───────┘
                    │ (views only)  │ scrub +   │ every step traced
                    │               │ audit     │
             ┌──────▼──────┐  ┌─────▼─────┐ ┌───▼──────┐
             │ Postgres    │  │ PHI layer │ │ Langfuse │
             │ deid VIEWS  │  │ class/    │ └──────────┘
             │ (RO role)   │  │ scrub/    │
             │             │  │ audit/leak│
             └─────────────┘  └───────────┘
   LiteLLM between every node and providers (cost/token/latency captured)
```

---

## 7. Repository structure

```
clinical-claims-analyst/
├── CLAUDE.md
├── README.md                      # decisions + PHI-safe design + metrics + demo link
├── docker-compose.yml             # postgres + langfuse + app
├── Dockerfile
├── pyproject.toml
├── .github/workflows/eval.yml     # CI: eval gate + PHI-leakage gate
├── src/
│   ├── agent/{graph,nodes,state,tools,prompts}.py
│   ├── phi/
│   │   ├── classification.py      # column → IDENTIFIER/QUASI/ANALYTIC
│   │   ├── scrub.py               # de-identification boundary
│   │   ├── audit.py               # append-only access log
│   │   └── leakage.py             # PHI-leakage scanner
│   ├── llm/client.py              # LiteLLM wrapper
│   ├── obs/tracing.py             # Langfuse helpers
│   ├── api/main.py                # FastAPI
│   └── data/
│       ├── load.py                # Synthea generate + load
│       └── views.sql              # de-identified views
├── eval/
│   ├── dataset.jsonl              # labeled clinical/claims questions
│   ├── scorers.py                 # result-set + execution + recovery + judge
│   ├── run_eval.py
│   ├── judge_validation.py        # judge vs human agreement
│   └── reports/
├── ui/
└── tests/                         # tools, scorers, PHI controls, graph transitions
```

---

## 8. Agent design

**State (`AgentState`, pydantic):** `question, schema_snapshot (views only), plan, candidate_sql, execution_result, execution_error, retry_count (max 3), final_answer, step_trace[]`.

**Nodes:**
1. **plan** — decompose; identify relevant view tables/columns; decide if the question is answerable as an aggregate (if it inherently needs row-level PHI, plan a safe aggregate or a graceful refusal).
2. **generate_sql** — single SELECT against **views**, aggregate-oriented, parameterized.
3. **execute_sql** (tool) — runs via the read-only role with statement timeout; **writes an audit record**; returns rows or structured error.
4. **validate** — branch: error / empty / row-level-PHI-shaped result → self_correct (if retries remain) else synthesize a graceful answer.
5. **self_correct** — feed failing SQL + DB error back; regenerate; increment retry_count; hard cap 3.
6. **synthesize** — aggregate result → cited NL answer that names the SQL run and the row count; passes through the scrubber; never emits an identifier.

**Tools:**
- `schema_inspect()` → **view** tables/columns/types/FKs only.
- `sql_execute(query)` → rows | structured error. Read-only, views-only, audited, timed out.

**Hard rules (enforced in code, not prompts):** read-only role on views only; reject non-SELECT and reject queries targeting base tables; statement timeout; retry cap 3 with graceful degradation; every node try/except records failure to state + Langfuse; every execution audited; every model input/output passes the leakage scanner.

---

## 9. Eval harness — the centerpiece

**9.1 Labeled dataset (`eval/dataset.jsonl`)** — start at **50 cases**, grow to 100+. Each:
```json
{
  "id": "q001",
  "question": "What is the 30-day all-cause readmission rate?",
  "gold_sql": "SELECT ...;",
  "gold_answer": "About 12.4% ...",
  "difficulty": "hard",
  "tags": ["readmission","date-window","self-join","quality-measure"]
}
```
Spread across difficulty and tags so you can report accuracy *by category*. Suggested healthcare question themes (these double as a demo script and show payer/quality fluency):
- **Cost:** average total claim cost per encounter by encounter class; total cost of care per patient (PMPM-style); cost share by payer in a given year.
- **Utilization:** ED visits per 1,000 patients; encounters per patient per year; top procedures by volume.
- **Clinical:** 10 most frequent conditions; average age (bucketed) of patients with Type 2 diabetes; immunization coverage rate.
- **Pharmacy:** top 10 medications by prescription count; polypharmacy prevalence.
- **Quality measures:** 30-day all-cause readmission rate; well-child visit rates; diabetic patients with an A1c observation in the last year.
- **Payer mix:** distribution of covered lives by payer; uninsured encounter rate.

**9.2 Scorers (`eval/scorers.py`)**
- **Result-set match (primary, deterministic):** execute `gold_sql` and the agent's SQL; compare result sets order-insensitive, type-normalized. Pass/fail per case.
- **Execution success:** did final SQL run?
- **Recovery:** of first-attempt SQL errors, fraction rescued by self_correct.
- **LLM-as-judge (secondary):** faithfulness of the NL answer to the result set + relevance, scored 1–5 on a rubric.
- **PHI-leakage (gate, not a score):** zero identifiers in any model input/output across the whole run. Any hit = hard fail.

**9.3 Judge validation (`eval/judge_validation.py`) — the rare senior move.**
Manually label ~30 answers yourself; measure judge-vs-human agreement (Cohen's kappa); tune the judge prompt to ≥0.70 and **report the number**. An unvalidated judge is just another unverified model output.

**9.4 Eval runner (`eval/run_eval.py`)** — runs the agent over the dataset, applies all scorers, writes `eval/reports/<timestamp>.{json,md}`: overall result-set accuracy, accuracy by difficulty/tag, recovery rate, p50/p95 latency, mean cost/query, judge score + judge-human agreement, and **PHI-leakage count (must be 0)**. Prompts version-tagged for comparability.

---

## 10. Observability

Langfuse session trace per request containing: each node, each LLM call (model/tokens/cost/latency via LiteLLM), each tool call (SQL + result/error), retry count, audit record id, final answer. Derived views: p50/p95 latency, cost/query, tool-error rate, retry distribution, failure clustering. When a demo query fails, you point to the exact trace and explain why — that narrative wins interviews.

---

## 11. API design (FastAPI)

- `POST /ask` → `{ "question": "..." }` → `{ answer, sql, row_count, latency_ms, cost_usd, trace_url, audit_id }`. Returning `sql`, `trace_url`, and `audit_id` makes the system legible *and* shows the audit trail in action.
- `GET /health` → liveness.
- `GET /` → demo UI.
- JSON in/out; no HTML `<form>` coupling.

---

## 12. Deployment

`docker-compose.yml` brings up Postgres + Langfuse + app locally. Production: containerize, deploy to Fly.io or Render (cheap always-on), managed Postgres. Point Langfuse at a cloud instance or Langfuse Cloud free tier. Put the reachable URL in the README.

---

## 13. CI/CD — the regression + safety gate

`.github/workflows/eval.yml`:
- On PR: unit tests (including PHI-control tests) → eval suite on a fixed subset (~25 cases, cheap models OK).
- **Gates:** fail if result-set accuracy drops below threshold (baseline − small margin), if any previously-passing case regresses, **or if the PHI-leakage scanner returns >0**.
- Post the eval report (incl. leakage=0) as a PR comment.

"CI gate blocks merges that degrade accuracy *or* introduce PHI leakage" is a uniquely healthcare-credible bullet.

---

## 14. Milestones (phased, with acceptance criteria)

**M0 — Skeleton (½–1 day).** Repo, deps, Docker, tiny Synthea pop in SQLite, FastAPI `/health` + stub `POST /ask`, LiteLLM calling one model, Langfuse trace working.
*Done when:* `POST /ask` returns a stub answer and the call shows as a Langfuse trace.

**M1 — Agent loop (1–2 days).** LangGraph graph, all six nodes, `schema_inspect` + read-only `sql_execute`, self-correction with cap, graceful failure.
*Done when:* 10 hand-tried questions answer correctly and a hard one survives self-correction, all visible in traces.

**M2 — PHI-safe layer (1–2 days).** Postgres + Synthea; classification map; de-identified views; read-only views-only role; scrubber; audit log; leakage scanner. Switch the agent to views.
*Done when:* the agent can only see/query views; every execution writes an audit record; the leakage scanner runs clean; a `SELECT *` row-level attempt is refused.

**M3 — Eval harness (1–2 days).** 50 labeled clinical/claims cases; result-set scorer; runner producing a report with accuracy by tag, recovery, latency, cost, leakage=0.
*Done when:* `python eval/run_eval.py` writes a report and you can state your baseline accuracy.

**M4 — Judge + validation (1 day).** LLM judge + manual labels + agreement measured/tuned.
*Done when:* the report includes a defensible judge-human agreement number.

**M5 — Optimize + measure (1 day).** Tune one real lever (model routing, prompt revision, schema-context trimming, caching). Re-run eval; record before/after delta.
*Done when:* a documented improvement, e.g. "−X% cost at equal accuracy" or "+Y pts on quality-measure questions."

**M6 — Ship + CI + write up (1 day).** Live URL; demo UI; GitHub Actions eval + leakage gate; README leading with metrics and the PHI-safe design; short writeup of the eval methodology and the safety architecture.
*Done when:* a stranger can hit the URL, and the README leads with numbers and the safety story.

---

## 15. Metrics & targets (your resume bullets)

| Metric | How measured | Sane initial target |
|---|---|---|
| Result-set accuracy | gold vs agent result-set match | report honestly; ~70%+ on medium respectable |
| Recovery rate | % of first-attempt SQL errors rescued | ≥50% |
| p95 latency | Langfuse, end-to-end | report; optimize in M5 |
| Cost / query | LiteLLM token cost | report; cut in M5 |
| Judge–human agreement | Cohen's kappa, M4 | ≥0.70 |
| **PHI leakage** | leakage scanner over all model I/O | **0, enforced in CI** |
| CI gate | accuracy floor + leakage=0 on PRs | = baseline − small margin, and 0 leakage |

Report the *real* numbers. "70% result-set accuracy, validated judge at 0.74 kappa, zero PHI leakage enforced in CI, on a synthetic claims+clinical dataset" is a far stronger sentence than any unverified claim.

---

## 16. `CLAUDE.md` (drop in repo root)

```markdown
# Clinical & Claims Analyst Agent — context for Claude Code

## What this is
An autonomous text-to-analytics agent over a SYNTHETIC claims+clinical dataset
(Synthea). It plans, generates SQL against de-identified VIEWS, executes,
self-corrects on error, and synthesizes a cited, PHI-safe answer. Priority order:
PHI-safe architecture > eval harness > observability > CI gates > agent cleverness.

## Stack (do not substitute without asking)
Python 3.12 · LangGraph · LiteLLM · Postgres · FastAPI · Langfuse (self-hosted)
· custom eval harness + pytest · GitHub Actions · Docker.

## Data
SYNTHETIC ONLY (Synthea). Never use or simulate real PHI. The repo must be safe
to make public.

## Hard rules
- The agent's DB role is READ-ONLY and can SELECT from DE-IDENTIFIED VIEWS ONLY,
  never base tables. Enforce at the DB grant level AND reject non-SELECT / base-
  table queries in code.
- No-PHI-to-model: schema_inspect exposes the VIEW schema only. Reject queries
  that return row-level individual records; support aggregate/analytic queries.
- Every sql_execute writes an append-only AUDIT record (request_id, sql, tables,
  row_count, outcome).
- Every model INPUT and OUTPUT passes the PHI leakage scanner; any hit is a hard
  failure.
- Self-correction retry cap = 3, then graceful "couldn't determine".
- Every node try/except records failure to state + Langfuse; never 500 the request.
- Prompts are version-tagged constants so eval reports are comparable.
- The agent never sees gold_sql — only the view schema and the question.
- No HTML <form> coupling; JSON in/out.

## Definition of done for any change
Unit tests pass (incl. PHI-control tests) AND `python eval/run_eval.py` runs,
does not regress result-set accuracy vs the last report, and reports PHI
leakage = 0.

## Build order
Follow milestones M0–M6 in AGENT_PROJECT_SPEC_HEALTHCARE.md. Start at M0. Get a
tiny Synthea population working in SQLite end-to-end before Postgres + views.
```

---

## 17. First prompt to give Claude Code

> Read `AGENT_PROJECT_SPEC_HEALTHCARE.md` and `CLAUDE.md` in full. Then execute **Milestone M0** only: scaffold the repo per §7, set up `pyproject.toml` with pinned deps, a Dockerfile and docker-compose with Postgres + Langfuse + the app, generate a tiny (~200 patient) Synthea population and load it into SQLite for now, stand up FastAPI with `/health` and a stub `POST /ask`, wire LiteLLM to call one model, and confirm one call appears as a Langfuse trace. Do NOT start M1, and do NOT wire Postgres/views yet. Treat all data as synthetic; the repo must be public-safe. When done, summarize what you built, how to run it locally, and show the Langfuse trace working. Ask me before substituting any tool in the committed stack.

Drive it milestone by milestone — review each before letting it move on. Note the build order deliberately puts the **PHI-safe layer (M2) before the eval harness (M3)**, because the eval must run against the de-identified views, and because the safety architecture is the part that most distinguishes this project. Total build is roughly 7–10 focused days; resist letting the agent loop consume the time that belongs to M2–M6.
