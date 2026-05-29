"""LLM-as-judge (spec §9.2 secondary scorer): scores how FAITHFUL an analytics answer is
to the query result (and how relevant to the question), 1-5 on a rubric.

The judge is itself a model output, so it is VALIDATED against human labels in
eval/judge_validation.py (Cohen's kappa) — an unvalidated judge is just another
unverified model output. Version-tagged so reports stay comparable.
"""

from __future__ import annotations

import json
import re

from src.llm import client as llm

JUDGE_VERSION = "judge-v1"

JUDGE_PROMPT_V1 = """\
You grade whether an analytics ANSWER faithfully and relevantly reflects the QUERY RESULT.
Judge ONLY faithfulness to the provided result and relevance to the question — do NOT
re-run any analysis or use outside knowledge.

Score 1-5:
5 = fully faithful: stated figures match the result; directly answers; no invented numbers.
4 = faithful with only minor rounding/phrasing imprecision; no wrong figures.
3 = partially faithful: on-topic but omits the key figure or is too vague to verify.
2 = mostly unfaithful: states a figure not supported by the result, or misreads it.
1 = unfaithful or irrelevant: invents/contradicts numbers, or does not answer the question.

A refusal or "couldn't determine" when the result clearly contains the answer scores 1.
Return ONLY JSON: {"score": <1-5 integer>, "reason": "<one short sentence>"}."""


def _render_result(result: dict | None, max_rows: int = 30) -> str:
    if not result or "rows" not in result:
        return "(no result / query failed)"
    cols = result.get("columns", [])
    rows = result.get("rows", [])[:max_rows]
    header = " | ".join(str(c) for c in cols)
    body = "\n".join(" | ".join("" if v is None else str(v) for v in row) for row in rows)
    return f"{header}\n{body}" if rows else f"{header}\n(0 rows)"


def _parse(text: str) -> tuple[int, str]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            score = int(data.get("score", 0))
            return (score if 1 <= score <= 5 else 0), str(data.get("reason", ""))[:200]
        except (ValueError, TypeError):
            pass
    digit = re.search(r"[1-5]", text or "")
    return (int(digit.group(0)) if digit else 0), (text or "")[:80]


def judge_answer(question: str, sql: str | None, result: dict | None, answer: str) -> dict:
    """Return {score:1-5, faithful:bool, reason, cost_usd}. score 0 = unparseable."""
    user = (
        f"QUESTION: {question}\n\n"
        f"SQL: {sql or '(none)'}\n\n"
        f"QUERY RESULT:\n{_render_result(result)}\n\n"
        f"ANSWER: {answer}"
    )
    res = llm.complete(
        [{"role": "system", "content": JUDGE_PROMPT_V1}, {"role": "user", "content": user}]
    )
    score, reason = _parse(res.text)
    return {
        "score": score,
        "faithful": score >= 4,
        "reason": reason,
        "cost_usd": res.cost_usd,
    }
