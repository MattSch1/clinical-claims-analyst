# Agentic Analyst — Engineering Spec

**Purpose:** Build, deploy, and rigorously evaluate an autonomous agent that answers natural-language questions over a real relational dataset by planning, generating SQL, executing it, recovering from its own errors, and synthesizing a cited answer. The agent is the easy half. The **eval harness, observability, and CI quality gates are the point** — they are what makes this a senior-signal portfolio artifact rather than a demo.

This document is written to be handed to Claude Code as the source of truth. It commits to specific tools so there is no ambiguity, and explains *why* each was chosen so the rationale survives into your README and interviews.

---

## 1. Why this project (and not a RAG chatbot)

The differentiator on a 2026 AI-engineering resume is not "I called an LLM." It is: *can you prove your system works, prove a change improved it, and recover from failure in production?* A text-to-analytics agent is the ideal vehicle because:

- **The task is genuinely agentic** — multi-step tool use, planning, state, and self-correction, not a single completion.
- **It has real, observable failure modes** — invalid SQL, hallucinated columns, wrong joins, empty result sets, timeouts.
- **Correctness is verifiable.** Because we can execute a ground-truth query, eval is not vibes-based. We get deterministic result-set scoring *plus* an LLM judge for the narrative — and we validate the judge against human labels. That validated-judge story is rare and highly hireable.
- **Every component emits a number** — accuracy, recovery rate, p95 latency, cost/query. Those numbers become resume bullets.

The architecture is **domain-portable**: swap the SQL tool for a code-execution tool and it becomes a code-fixing agent; swap it for web search + fetch and it becomes a research agent. The eval/observability/CI scaffolding is identical. Build it once here.

---

## 2. Goals / Non-goals

**Goals**
- An autonomous agent with explicit state, tool use, and automatic error recovery.
- A custom eval harness with a labeled dataset, deterministic scorers, and an LLM-as-judge **validated against human labels**.
- Full tracing + cost/latency observability on every run.
- A regression gate in CI that blocks merges degrading quality below a threshold.
- A live, reachable HTTP endpoint + a minimal demo UI.
- A README and a short writeup documenting decisions and the measured numbers.

**Non-goals**
- A polished multi-tenant product. Single dataset, single user is fine.
- Fine-tuning. Use hosted models via a gateway.
- Beating SOTA text-to-SQL benchmarks. The eval *methodology* is the deliverable, not the leaderboard score.
- Multi-agent orchestration. One well-instrumented agent beats a swarm of unmeasured ones.

---

## 3. Tech stack (committed, with rationale)

| Layer | Choice | Why this one |
|---|---|---|
| Language | Python 3.12 | Default for the role; type hints throughout. |
| Agent orchestration | **LangGraph** | Most battle-tested for stateful agents; explicit graph = inspectable control flow; matches what job descriptions list. The ReAct loop logic is still written by hand inside nodes (see §6) so understanding is visible, not hidden behind abstraction. |
| Model access | **LiteLLM** | Provider-agnostic gateway. Gives multi-provider fallback, a single call site, and per-call cost/token accounting for free. Demonstrates you abstract the provider. |
| Models | One strong (e.g. a frontier model) for planning/synthesis; one cheap/fast for routing/simple steps | Lets you show a cost/latency tradeoff you actually tuned, not just "used the biggest model." |
| Data store | **Postgres** (Dockerized) | Real joins, real types, real failure modes. SQLite acceptable for first run. |
| Serving | **FastAPI** | Standard, async, OpenAPI docs for free. **No** HTML `<form>`-based coupling; clean JSON API. |
| Observability | **Langfuse** (self-hosted via Docker) | Open-source, MIT, free, OpenTelemetry-aligned. Self-hosting it is itself an infra signal. Traces every LLM call, tool call, and the full agent session. |
| Eval harness | **Custom** (you own the logic) + pytest runner | Owning the scoring logic is the differentiator skill. Deterministic scorers + LLM judge live in your repo, not a vendor's. |
| CI | **GitHub Actions** | Runs the eval suite on PRs and gates merges on a regression threshold. |
| Deploy | **Docker** + Fly.io or Render (cheap always-on) | A reachable URL matters more than the host. Serverless is fine if cold starts are acceptable. |
| Demo UI | Minimal React or plain HTML page hitting the API | Recruiters engage far more with a live demo than a repo alone. |

**Decision to document in your README (this sentence *is* the skill):** "I chose Langfuse over LangSmith for self-hosted data ownership and no per-trace pricing; I chose LangGraph over CrewAI because I wanted explicit, inspectable state transitions and durable control flow rather than role-based abstraction."

**Optional managed alternative:** If you'd rather not self-host eval infra, Braintrust is eval-native with CI quality gates and a generous free tier. Trade-off: less 'I built it' signal, faster setup. Default spec assumes the custom harness.

---

## 4. High-level architecture

```
                ┌─────────────┐
   HTTP  ─────► │  FastAPI    │ ──► request_id, question
                └──────┬──────┘
                       │
                ┌──────▼───────────────────────────────┐
                │  LangGraph Agent (stateful)           │
                │                                       │
                │  plan ─► generate_sql ─► execute_sql  │
                │            ▲                │         │
                │            │   on error     ▼         │
                │         self_correct ◄── validate     │
                │                                │       │
                │                             synthesize │
                └──────┬────────────────────────┬───────┘
                       │ tool calls             │ every step traced
                ┌──────▼──────┐          ┌──────▼──────┐
                │  Postgres   │          │  Langfuse   │
                └─────────────┘          └─────────────┘

   LiteLLM sits between every node and the model providers
   (cost + token + latency captured per call).
```

The agent is a graph with explicit nodes and a bounded self-correction loop (hard cap on retries). State carries: question, schema snapshot, plan, candidate SQL, execution result/error, retry count, final answer, and a trace of every step.

---

## 5. Repository structure

```
agentic-analyst/
├── CLAUDE.md                  # context file for Claude Code (see §15)
├── README.md                  # decisions + measured numbers + demo link
├── docker-compose.yml         # postgres + langfuse + app
├── Dockerfile
├── pyproject.toml             # uv or poetry; pinned deps
├── .github/workflows/eval.yml # CI eval gate
├── src/
│   ├── agent/
│   │   ├── graph.py           # LangGraph state machine
│   │   ├── nodes.py           # plan / generate_sql / execute / validate / correct / synthesize
│   │   ├── state.py           # typed AgentState (pydantic)
│   │   ├── tools.py           # sql_execute, schema_inspect
│   │   └── prompts.py         # versioned prompts (string constants + version tag)
│   ├── llm/
│   │   └── client.py          # LiteLLM wrapper: routing, fallback, cost capture
│   ├── obs/
│   │   └── tracing.py         # Langfuse instrumentation helpers
│   ├── api/
│   │   └── main.py            # FastAPI app
│   └── data/
│       └── load.py            # dataset download + schema load
├── eval/
│   ├── dataset.jsonl          # labeled eval cases (see §7)
│   ├── scorers.py             # deterministic + LLM-judge scorers
│   ├── run_eval.py            # runs agent over dataset, writes report
│   ├── judge_validation.py    # measures judge-vs-human agreement
│   └── reports/               # generated metric reports (json + md)
├── ui/                        # minimal demo page
└── tests/                     # unit tests for tools, scorers, graph transitions
```

---

## 6. Dataset

**Default: the Olist Brazilian E-Commerce dataset** (public, ~9 related tables: orders, order_items, products, customers, sellers, payments, reviews, geolocation). It is multi-table and join-heavy, which produces the rich failure modes that make eval interesting.

- `src/data/load.py` downloads the CSVs, creates the Postgres schema, and bulk-loads. Idempotent; safe to re-run.
- **Quick-start fallback:** the Chinook SQLite database (single file, no download friction) so Claude Code can get the loop working end-to-end before wiring Postgres.

The agent must **never** see ground-truth SQL — only the live schema (via the `schema_inspect` tool) and the question.

---

## 7. Agent design

**State (`AgentState`, pydantic):**
`question, schema_snapshot, plan, candidate_sql, execution_result, execution_error, retry_count (max 3), final_answer, step_trace[]`

**Nodes:**
1. **plan** — decompose the question; decide which tables/columns are relevant from the schema snapshot. Output a short structured plan.
2. **generate_sql** — produce a single SQL query from the plan + schema. Parameterized; read-only enforced.
3. **execute_sql** (tool) — run against Postgres with a statement timeout. Returns rows or a structured error.
4. **validate** — branch: if error or empty/implausible result → route to self_correct (if retries remain) else → synthesize with a graceful "couldn't determine" answer.
5. **self_correct** — feed the failing SQL + the DB error back in, regenerate. Increment retry_count. Hard cap at 3, then give up gracefully.
6. **synthesize** — turn the result set into a natural-language answer that **cites the SQL it ran and the row count** (the "evidence"). No evidence → no confident claim.

**Tools:**
- `schema_inspect()` → tables, columns, types, FK relationships.
- `sql_execute(query)` → rows | structured error. **Read-only** (reject anything but SELECT; run as a least-privilege DB role).

**Hard safety/robustness rules (must be enforced in code, not just prompts):**
- Read-only DB role; reject non-SELECT at the tool layer.
- Statement timeout (e.g. 10s) so a bad query can't hang.
- Retry cap (3) with graceful degradation.
- Every node wrapped in try/except that records the failure to state and to Langfuse rather than crashing the request.

---

## 8. Eval harness — the centerpiece

**8.1 Labeled dataset (`eval/dataset.jsonl`)** — start with **50 cases**, grow to 100+. Each case:
```json
{
  "id": "q001",
  "question": "Which product category generated the most revenue in 2017?",
  "gold_sql": "SELECT ... ;",
  "gold_answer": "Health & beauty, with R$ ...",
  "difficulty": "medium",
  "tags": ["aggregation", "join", "date-filter"]
}
```
Spread across difficulty and SQL feature tags so you can report accuracy *by category* (e.g. "joins are my weak spot") — that breakdown is high-signal.

**8.2 Scorers (`eval/scorers.py`)**
- **Deterministic — result-set match (primary):** execute `gold_sql`, execute the agent's SQL, compare result sets (order-insensitive, type-normalized). This is the rigorous, non-gameable metric. Pass/fail per case.
- **Deterministic — execution success:** did the agent's final SQL run at all?
- **Recovery:** of cases where the first generated SQL errored, what fraction did self_correct rescue?
- **LLM-as-judge — answer quality (secondary):** judge the natural-language answer for faithfulness to the result set and relevance to the question. Structured rubric, scored 1–5.

**8.3 Judge validation (`eval/judge_validation.py`) — the rare, senior move**
You **manually label** ~30 answers yourself (or have a friend do a subset for inter-rater signal), then measure the LLM judge's agreement with your human labels (Cohen's kappa or simple % agreement). Report it. A judge you haven't validated is just another unverified model output; a validated judge is evidence you understand evaluation. Tune the judge prompt until agreement is acceptable (aim ≥0.7 kappa) and **report the final agreement number**.

**8.4 Eval runner (`eval/run_eval.py`)**
Runs the agent over the whole dataset, applies all scorers, and writes `eval/reports/<timestamp>.{json,md}` with: overall result-set accuracy, accuracy by difficulty and tag, recovery rate, mean/p50/p95 latency, mean cost/query, and judge score + judge-human agreement. Prompts are version-tagged so reports are comparable across changes.

---

## 9. Observability

Instrument with Langfuse so a single request produces one **session trace** containing: each node, each LLM call (model, tokens, cost, latency via LiteLLM), each tool call (the SQL and its result/error), retry count, and the final answer.

Surface as dashboards/derived metrics: p50/p95 end-to-end latency, cost per query, tool-error rate, retry distribution, and a clusterable view of failures. The goal: when a query fails in the demo, you can point to the exact trace and explain why. That narrative wins interviews.

---

## 10. API design (FastAPI)

- `POST /ask` → `{ "question": "..." }` → `{ answer, sql, row_count, latency_ms, cost_usd, trace_url }`. Returning `sql` + `trace_url` makes the system legible to anyone testing it.
- `GET /health` → liveness.
- `GET /` → serves the minimal demo UI.
- Stream tokens if easy; not required.
- No HTML `<form>` coupling — JSON in, JSON out; the UI calls it with `fetch`.

---

## 11. Deployment

- `docker-compose.yml` brings up Postgres + Langfuse + the app for local/full-stack runs.
- Production: containerize the app, deploy to Fly.io or Render for cheap always-on hosting; managed Postgres or a small attached volume. Point Langfuse at the cloud instance or Langfuse Cloud free tier if you don't want to host it in prod.
- Put a real, reachable URL in the README. A live demo is worth more than the repo.

---

## 12. CI/CD — the regression gate

`.github/workflows/eval.yml`:
- On PR: run unit tests, then run the eval suite against a **fixed subset** (e.g. 25 cases, cheap models acceptable for the gate).
- **Gate:** fail the job if result-set accuracy drops below a threshold (start at your current baseline minus a small margin) or if any previously-passing case regresses.
- Post the eval report as a PR comment.
- This is the bullet "added CI eval gates blocking merges that degrade quality." Make it real, not aspirational.

---

## 13. Milestones (phased build plan with acceptance criteria)

**M0 — Skeleton (½ day).** Repo, deps, Docker, Chinook loaded, FastAPI `/health` up, LiteLLM calling one model, Langfuse capturing a hello-world trace.
*Done when:* `POST /ask` returns a hard-coded answer and the call appears as a Langfuse trace.

**M1 — Agent loop (1–2 days).** LangGraph graph with all six nodes; `schema_inspect` + read-only `sql_execute`; self-correction with retry cap; graceful failure.
*Done when:* 10 hand-tried questions return correct answers and a deliberately hard one triggers and survives self-correction, all visible in traces.

**M2 — Eval harness (1–2 days).** 50 labeled cases; result-set scorer; runner producing a report with accuracy by tag, recovery rate, latency, cost.
*Done when:* `python eval/run_eval.py` writes a report and you can state your baseline accuracy.

**M3 — Judge + validation (1 day).** LLM judge for answer quality; manual labels; agreement measured and tuned.
*Done when:* the report includes a judge-human agreement number you'd defend in an interview.

**M4 — Optimize + measure (1 day).** Tune at least one real lever (model routing cheap-vs-strong, prompt revision, schema-context trimming, caching). Re-run eval. Record before/after numbers.
*Done when:* you have a documented delta, e.g. "−X% cost at equal accuracy" or "+Y pts accuracy on joins."

**M5 — Ship + CI + write up (1 day).** Deploy live URL; demo UI; GitHub Actions eval gate; README with decisions, metrics table, and demo link; short writeup of the eval methodology.
*Done when:* a stranger can hit the URL, and the README leads with numbers.

---

## 14. Metrics & targets (these become your resume bullets)

| Metric | How measured | Sane initial target |
|---|---|---|
| Result-set accuracy | gold vs agent result set match | report honestly; ~70%+ on medium is respectable |
| Recovery rate | % of first-attempt SQL errors rescued by self-correct | ≥50% |
| p95 latency | Langfuse, end-to-end | report; optimize in M4 |
| Cost / query | LiteLLM token cost | report; cut in M4 via routing |
| Judge–human agreement | Cohen's kappa, M3 | ≥0.70 |
| CI gate | accuracy floor on PRs | = baseline − small margin |

Report the *real* numbers. "70% accuracy with a validated judge and a CI gate" is more credible and more impressive than an unverified "works great."

---

## 15. `CLAUDE.md` (drop this in the repo root)

```markdown
# Agentic Analyst — context for Claude Code

## What this is
An autonomous text-to-analytics agent over a Postgres dataset. It plans,
generates SQL, executes it, self-corrects on error, and synthesizes a cited
answer. The eval harness, observability, and CI gates are the priority, not
agent cleverness.

## Stack (do not substitute without asking)
Python 3.12 · LangGraph · LiteLLM · Postgres · FastAPI · Langfuse (self-hosted)
· custom eval harness + pytest · GitHub Actions · Docker.

## Hard rules
- SQL tool is READ-ONLY: reject non-SELECT at the code layer; use a
  least-privilege DB role; enforce a statement timeout.
- Self-correction retry cap = 3, then graceful "couldn't determine".
- Every node has try/except that records failure to state + Langfuse; a single
  failure must never 500 the request.
- Prompts are version-tagged constants in prompts.py so eval reports are
  comparable across changes.
- The agent never sees gold_sql — only the live schema and the question.
- No HTML <form> coupling in the API; JSON in/out.

## Definition of done for any change
Unit tests pass AND `python eval/run_eval.py` runs and does not regress
result-set accuracy vs the last report in eval/reports/.

## Build order
Follow milestones M0–M5 in AGENT_PROJECT_SPEC.md. Start at M0. Get the Chinook
SQLite path working end-to-end before wiring Postgres + Olist.
```

---

## 16. First prompt to give Claude Code

> Read `AGENT_PROJECT_SPEC.md` and `CLAUDE.md` in full. Then execute **Milestone M0** only: scaffold the repo per §5, set up `pyproject.toml` with pinned deps, a Dockerfile and docker-compose with Postgres + Langfuse + the app, load the Chinook SQLite DB, stand up a FastAPI service with `/health` and a `POST /ask` that returns a hard-coded answer, wire LiteLLM to call one model, and confirm one call shows up as a Langfuse trace. Do not start M1. When done, summarize what you built, how to run it locally, and show me the Langfuse trace working. Ask me before substituting any tool in the committed stack.

Drive it milestone by milestone — review M0 before letting it touch M1. The whole build is roughly 6–9 focused days; the eval and CI work (M2–M5) is where the resume value concentrates, so don't let the agent loop eat all the time.
