"""Judge validation (spec §9.3) — the rare senior move.

Runs the LLM judge over a human-labeled set (eval/judge_labels.jsonl) and measures
judge-vs-human agreement with Cohen's kappa on the binary "faithful" decision
(score >= 4). Target kappa >= 0.70; report the number. An unvalidated judge is just
another unverified model output.

The labels are author-provided reference labels spanning the faithfulness spectrum
(clearly-faithful → invented/contradictory/refused) so kappa is non-degenerate; review
and adjust them to your own judgment. Run from the repo root:

    python eval/judge_validation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root for `from src...`

import json  # noqa: E402
from collections import Counter  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import judge  # noqa: E402  (eval/ is on sys.path as the script dir)

from src.config import get_settings  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
LABELS = EVAL_DIR / "judge_labels.jsonl"
REPORTS = EVAL_DIR / "reports"
TARGET_KAPPA = 0.70


def cohen_kappa(a: list, b: list) -> float:
    """Cohen's kappa for two equal-length lists of categorical labels."""
    n = len(a)
    if n == 0:
        return 0.0
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca.get(c, 0) / n) * (cb.get(c, 0) / n) for c in set(a) | set(b))
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def main() -> int:
    settings = get_settings()
    if not settings.llm_ready and not settings.llm_mock:
        print("Judge validation needs a model (set OPENAI_API_KEY or LLM_MOCK).")
        return 2

    labels = [json.loads(line) for line in LABELS.read_text().splitlines() if line.strip()]
    print(f"Validating judge ({judge.JUDGE_VERSION}) on {len(labels)} human-labeled answers\n")

    human_faithful, judge_faithful, exact_hits, cases = [], [], 0, []
    for ex in labels:
        verdict = judge.judge_answer(ex["question"], ex.get("sql"), ex.get("result"), ex["answer"])
        h_score, j_score = ex["human_score"], verdict["score"]
        h_faith, j_faith = h_score >= 4, verdict["faithful"]
        human_faithful.append(h_faith)
        judge_faithful.append(j_faith)
        exact_hits += int(h_score == j_score)
        agree = h_faith == j_faith
        cases.append({"id": ex["id"], "human": h_score, "judge": j_score, "agree": agree})
        print(f"  {ex['id']}: human={h_score} judge={j_score} {'OK' if agree else 'DISAGREE'}")

    n = len(labels)
    kappa = cohen_kappa(human_faithful, judge_faithful)
    binary_agreement = sum(c["agree"] for c in cases) / n
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "judge_version": judge.JUDGE_VERSION,
        "model": settings.llm_model,
        "n_labels": n,
        "cohen_kappa_faithful": round(kappa, 3),
        "target_kappa": TARGET_KAPPA,
        "meets_target": kappa >= TARGET_KAPPA,
        "binary_agreement": round(binary_agreement, 3),
        "exact_score_agreement": round(exact_hits / n, 3),
        "cases": cases,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (REPORTS / f"judge_validation_{stamp}.json").write_text(json.dumps(report, indent=2))

    verdict = "MEETS TARGET" if kappa >= TARGET_KAPPA else "BELOW TARGET — tune the rubric"
    print(
        f"\n=== Cohen's kappa (faithful>=4) = {kappa:.3f} vs target {TARGET_KAPPA} "
        f"[{verdict}] | binary agreement {binary_agreement:.0%} | "
        f"exact 1-5 agreement {exact_hits / n:.0%} ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
