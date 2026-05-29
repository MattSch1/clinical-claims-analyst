# Clinical & Claims Analyst Agent — context for Claude Code

## What this is
An autonomous text-to-analytics agent over a SYNTHETIC claims+clinical dataset
(Synthea). It plans, generates SQL against de-identified VIEWS, executes,
self-corrects on error, and synthesizes a cited, PHI-safe answer. Priority order:
PHI-safe architecture > eval harness > observability > CI gates > agent cleverness.

Full spec is in AGENT_PROJECT_SPEC_HEALTHCARE.md. Read it before acting.

## Stack (do not substitute without asking)
Python 3.12 · LangGraph · LiteLLM · Postgres · FastAPI · Langfuse (self-hosted)
· custom eval harness + pytest · GitHub Actions · Docker.

## Data
SYNTHETIC ONLY (Synthea). Never use or simulate real PHI. The repo must be safe
to make public.

## Starter files already in place (do not rewrite without asking)
- src/phi/classification.py  — HIPAA Safe Harbor data-classification map
- src/data/views.sql         — de-identified views with per-patient date-shifting
- eval/dataset.jsonl         — 20 labeled clinical/claims eval cases (gold SQL
                               targets the v_* views)
These are wired in at M2 (views/classification) and M3 (eval dataset). Leave
them alone until then.

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
Do not jump ahead; finish and confirm each milestone before starting the next.
