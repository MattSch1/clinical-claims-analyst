"""Eval runner (spec §9.4).

Runs the agent over eval/dataset.jsonl (the agent NEVER sees gold_sql — only the view
schema + the question), scores each case by result-set match against the executed gold
SQL, and writes a timestamped report (json + md) to eval/reports/ with: overall accuracy,
accuracy by difficulty/tag, recovery rate, p50/p95 latency, mean cost/query, and the
PHI-leakage count (target 0). Prompts are version-tagged so reports stay comparable.

Requires DATA_BACKEND=postgres (the de-identified views). Run from the repo root:

    python eval/run_eval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root for `from src...`

import json  # noqa: E402
import os  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import judge  # noqa: E402  (eval/ is on sys.path as the script dir)
import psycopg  # noqa: E402
import scorers  # noqa: E402

from src.agent.graph import run_agent  # noqa: E402
from src.agent.prompts import PROMPT_VERSION  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.data import db  # noqa: E402
from src.phi import leakage  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
DATASET = EVAL_DIR / "dataset.jsonl"
REPORTS = EVAL_DIR / "reports"


def _run_gold(sql_text: str) -> dict:
    """Execute the trusted gold SQL against the de-identified views (owner connection).

    "Trusted" is verified, not assumed: gold must be a single SELECT over the v_* views
    only — a dataset edit that drifted to a base table would otherwise silently run with
    owner privileges."""
    db.assert_select_only(sql_text)
    db.assert_relations_allowlisted(sql_text)
    dsn = get_settings().analytics_owner_dsn
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql_text)
        columns = [d.name for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()]
        return {"columns": columns, "rows": rows, "row_count": len(rows)}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))
    return ordered[idx]


def main() -> int:
    settings = get_settings()
    if not settings.use_postgres:
        print("Eval requires DATA_BACKEND=postgres (the de-identified views). Aborting.")
        return 2
    if not settings.audit_writer_dsn:
        # sql_execute fails closed without an audit writer, so every case would return
        # 'audit log unavailable' and accuracy would read 0 — fail loud instead (mirrors
        # the API's startup check) so the gated metric can't be silently zeroed.
        print("Eval needs AUDIT_WRITER_DSN (sql_execute fails closed without it). Aborting.")
        return 2

    cases = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    print(
        f"Running {len(cases)} eval cases on prompts={PROMPT_VERSION} "
        f"model={settings.llm_model}\n"
    )

    per_case = []
    for case in cases:
        t0 = time.perf_counter()
        state = run_agent(case["question"], request_id=f"eval-{case['id']}")
        latency_ms = (time.perf_counter() - t0) * 1000.0

        agent_result = state.execution_result
        try:
            gold_result = _run_gold(case["gold_sql"])
            gold_error = None
        except Exception as exc:  # noqa: BLE001 - a broken gold query shouldn't abort the run
            gold_result, gold_error = None, f"{type(exc).__name__}: {exc}"

        matched = scorers.result_set_match(gold_result, agent_result)
        leaks = list(state.leakage_hits) + [
            f"answer:{lk.kind}" for lk in leakage.scan_text(state.final_answer or "")
        ]
        # Secondary scorer: LLM judge rates the answer's faithfulness (validated at
        # kappa via eval/judge_validation.py). One failed judge call must not abort
        # the whole run — score 0 (excluded from the mean) and keep going.
        try:
            verdict = judge.judge_answer(
                case["question"], state.candidate_sql, agent_result, state.final_answer or ""
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  {case['id']} judge call failed (non-fatal): {exc}")
            verdict = {"score": 0, "faithful": False, "reason": f"judge error: {exc}"}
        per_case.append({
            "id": case["id"],
            "difficulty": case.get("difficulty"),
            "tags": case.get("tags", []),
            "matched": matched,
            "executed": scorers.execution_ok(agent_result),
            "status": state.status,
            "self_corrections": state.retry_count,
            "latency_ms": round(latency_ms, 1),
            "cost_usd": round(state.total_cost_usd, 6),
            "judge_score": verdict["score"],
            "leakage": leaks,
            "agent_sql": state.candidate_sql,
            "gold_error": gold_error,
        })
        flag = "PASS" if matched else "MISS"
        print(f"  {case['id']} [{flag}] status={state.status} retries={state.retry_count}")

    n = len(per_case)
    matched_n = sum(r["matched"] for r in per_case)
    first_errors = [r for r in per_case if r["self_corrections"] > 0]
    recovered = [r for r in first_errors if r["matched"] or r["status"] in ("ok", "recovered")]
    latencies = [r["latency_ms"] for r in per_case]
    judge_scores = [r["judge_score"] for r in per_case if r["judge_score"] > 0]

    by_diff: dict[str, list[bool]] = {}
    by_tag: dict[str, list[bool]] = {}
    for r in per_case:
        by_diff.setdefault(r["difficulty"] or "unknown", []).append(r["matched"])
        for tag in r["tags"]:
            by_tag.setdefault(tag, []).append(r["matched"])

    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "prompt_version": PROMPT_VERSION,
        "model": settings.llm_model,
        "sql_model": settings.sql_model_resolved,
        "n_cases": n,
        "result_set_accuracy": round(matched_n / n, 3) if n else 0.0,
        "execution_success_rate": round(sum(r["executed"] for r in per_case) / n, 3) if n else 0.0,
        "recovery_rate": round(len(recovered) / len(first_errors), 3) if first_errors else None,
        "recovery_detail": f"{len(recovered)}/{len(first_errors)} first-attempt errors rescued",
        "p50_latency_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95_latency_ms": round(_percentile(latencies, 0.95), 1),
        "mean_cost_usd": round(sum(r["cost_usd"] for r in per_case) / n, 6) if n else 0.0,
        "total_cost_usd": round(sum(r["cost_usd"] for r in per_case), 6),
        "phi_leakage_count": sum(len(r["leakage"]) for r in per_case),
        "mean_judge_score": round(statistics.mean(judge_scores), 2) if judge_scores else 0.0,
        "judge_version": judge.JUDGE_VERSION,
        "accuracy_by_difficulty": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_diff.items())},
        "accuracy_by_tag": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_tag.items())},
        "cases": per_case,
    }

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (REPORTS / f"{stamp}.json").write_text(json.dumps(report, indent=2))
    (REPORTS / f"{stamp}.md").write_text(_render_md(report))

    print(
        f"\n=== {report['result_set_accuracy']:.0%} result-set accuracy ({matched_n}/{n}) | "
        f"recovery {report['recovery_detail']} | p95 {report['p95_latency_ms']:.0f}ms | "
        f"${report['mean_cost_usd']:.5f}/query | PHI leakage {report['phi_leakage_count']} ==="
    )
    print(f"report: eval/reports/{stamp}.md")

    # CI gate: fail on ANY PHI leakage, or accuracy below the floor (baseline - margin).
    # EVAL_MIN_ACCURACY defaults to 0 (no accuracy gate locally); CI sets it explicitly.
    floor = float(os.getenv("EVAL_MIN_ACCURACY", "0") or "0")
    if report["phi_leakage_count"] > 0:
        print(f"GATE FAIL: PHI leakage = {report['phi_leakage_count']} (must be 0)")
        return 1
    if report["result_set_accuracy"] < floor:
        print(
            f"GATE FAIL: result-set accuracy {report['result_set_accuracy']:.0%} "
            f"< floor {floor:.0%}"
        )
        return 1
    return 0


def _render_md(r: dict) -> str:
    lines = [
        f"# Eval report — {r['timestamp']}",
        "",
        f"- **Prompts:** `{r['prompt_version']}`  · **Model:** `{r['model']}`  · "
        f"**Cases:** {r['n_cases']}",
        f"- **Result-set accuracy:** **{r['result_set_accuracy']:.1%}**",
        f"- **Execution success:** {r['execution_success_rate']:.1%}",
        f"- **Recovery rate:** {r['recovery_detail']}",
        f"- **Latency:** p50 {r['p50_latency_ms']:.0f} ms · p95 {r['p95_latency_ms']:.0f} ms",
        f"- **Cost:** ${r['mean_cost_usd']:.5f}/query (${r['total_cost_usd']:.4f} total)",
        f"- **PHI leakage:** {r['phi_leakage_count']} (target 0)",
        f"- **Mean judge score:** {r['mean_judge_score']}/5 (`{r['judge_version']}`, "
        "faithfulness; validated separately via judge_validation.py)",
        "",
        "## Accuracy by difficulty",
        *[f"- {k}: {v}" for k, v in r["accuracy_by_difficulty"].items()],
        "",
        "## Accuracy by tag",
        *[f"- {k}: {v}" for k, v in r["accuracy_by_tag"].items()],
        "",
        "## Per-case",
        "| id | match | status | retries | latency ms | gold_error |",
        "|----|-------|--------|---------|-----------|-----------|",
    ]
    for c in r["cases"]:
        lines.append(
            f"| {c['id']} | {'✅' if c['matched'] else '❌'} | {c['status']} | "
            f"{c['self_corrections']} | {c['latency_ms']:.0f} | {c['gold_error'] or ''} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
