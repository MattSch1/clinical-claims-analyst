"""M1 acceptance demo: run a set of clinical/claims questions through the agent and
print a compact report (answer, SQL, rows, self-corrections, cost, trace URL).

Run from the repo root with the stack up and a funded key (or LLM_MOCK=true):

    python scripts/m1_demo.py                 # the built-in question set
    python scripts/m1_demo.py "your question" # a single custom question
"""

from __future__ import annotations

import sys
import textwrap

from src.agent.graph import run_agent
from src.config import get_settings
from src.obs import tracing

QUESTIONS = [
    "How many patients are in the dataset?",
    "What is the average total claim cost per encounter, broken down by encounter class?",
    "What are the 10 most frequent conditions by number of patients?",
    "What are the top 10 medications by prescription count?",
    "How many encounters are there of each encounter class?",
    "What is the total cost of care (sum of total_claim_cost) by payer name?",
    "What is the average age of patients, in years?",
    "What is the immunization count by vaccine description?",
    "What share of all encounters are emergency (ED) visits?",
    # Hard one — self-join + 30-day date window; likely to need self-correction.
    "What is the 30-day all-cause hospital readmission rate?",
]


def _run_one(question: str):
    with tracing.span(
        name="ask", input=question, trace_name="m1_demo", tags=["m1", "demo"]
    ) as root:
        state = run_agent(question)
        root["output"] = state.final_answer
    return state, tracing.trace_url(root.get("trace_id"))


def main(argv: list[str]) -> int:
    s = get_settings()
    questions = [argv[1]] if len(argv) > 1 else QUESTIONS
    print(f"model={s.llm_model}  mock={s.llm_mock}  langfuse_ready={s.langfuse_ready}\n")

    answered = recovered = 0
    for i, q in enumerate(questions, 1):
        state, url = _run_one(q)
        ok = state.status in ("ok", "recovered")
        answered += int(ok)
        recovered += int(ok and state.retry_count > 0)
        print(f"[{i}] Q: {q}")
        print(f"    SQL: {state.candidate_sql}")
        print(
            f"    rows={state.row_count}  status={state.status}  "
            f"self_corrections={state.retry_count}  cost=${state.total_cost_usd:.5f}"
        )
        print(f"    A: {textwrap.shorten(state.final_answer or '', width=320)}")
        print(f"    trace: {url}\n")

    tracing.flush()
    print(f"summary: {answered}/{len(questions)} answered; {recovered} after self-correction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
