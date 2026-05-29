"""Eval scorers (spec §9.2).

The primary, deterministic scorer is the result-set match: execute the gold SQL and the
agent's SQL and compare the result sets. The comparison is:
  * row-order insensitive (multiset of rows),
  * column-order insensitive within a row (values are sorted) — so `gender, n` matches
    `n, gender`,
  * type-normalized: ints/floats/Decimals compared as floats rounded to 1 dp — this
    tolerates the common gold-vs-agent ROUND-precision mismatch (e.g. gold ROUND(...,1)
    = 32.3 vs agent ROUND(...,2) = 32.26 are the same answer); everything else by
    stripped string. (1 dp is safe here: the aggregate answers are well-separated, so it
    does not falsely match genuinely different results like 1000 vs 1551.6.)

Execution success, recovery, and PHI-leakage are simple aggregates computed by the
runner; the only non-trivial logic lives here.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal


def _norm_value(v: object) -> object:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return float(v)
    if isinstance(v, (float, Decimal)):
        return round(float(v), 1)
    return str(v).strip()


def _norm_row(row) -> tuple:
    # repr so mixed types (None / float / str) sort deterministically.
    return tuple(sorted(repr(_norm_value(v)) for v in row))


def result_set_match(gold: dict | None, agent: dict | None) -> bool:
    """True iff the two result sets are equal as order-insensitive, type-normalized
    multisets of rows."""
    if not gold or not agent or "rows" not in gold or "rows" not in agent:
        return False
    gold_rows = Counter(_norm_row(r) for r in gold["rows"])
    agent_rows = Counter(_norm_row(r) for r in agent["rows"])
    return gold_rows == agent_rows


def execution_ok(agent_result: dict | None) -> bool:
    """Did the agent's final SQL run and return a result set?"""
    return bool(agent_result and "rows" in agent_result)
