"""Eval runner — implemented in M3.

Runs the agent over eval/dataset.jsonl, applies all scorers, and writes a
timestamped report (accuracy by tag, recovery, latency, cost, PHI-leakage=0)
to eval/reports/. The agent never sees gold_sql — only the view schema and the
question.

M0 status: the eval harness is not implemented yet (it must run against the
de-identified views created in M2). This entrypoint exists so the repo layout
and the `python eval/run_eval.py` contract are in place; it is a no-op until M3.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "[run_eval] Eval harness is implemented in M3 (see "
        "AGENT_PROJECT_SPEC_HEALTHCARE.md §9). M0 is the skeleton milestone; "
        "no eval cases are scored yet."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
