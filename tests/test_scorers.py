"""Unit tests for the primary deterministic scorer (eval/scorers.py) — the number the
CI gate trusts, so it gets its own coverage: row-order, column-order, float tolerance,
and the must-not-match cases."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import scorers  # noqa: E402


def _rs(columns, rows):
    return {"columns": columns, "rows": rows, "row_count": len(rows)}


def test_match_is_row_order_insensitive():
    a = _rs(["gender", "n"], [["F", 10], ["M", 12]])
    b = _rs(["gender", "n"], [["M", 12], ["F", 10]])
    assert scorers.result_set_match(a, b)


def test_match_is_column_order_insensitive():
    a = _rs(["gender", "n"], [["F", 10]])
    b = _rs(["n", "gender"], [[10, "F"]])
    assert scorers.result_set_match(a, b)


def test_match_tolerates_rounding_to_one_decimal():
    a = _rs(["avg"], [[Decimal("32.3")]])
    b = _rs(["avg"], [[32.26]])
    assert scorers.result_set_match(a, b)
    assert not scorers.result_set_match(_rs(["avg"], [[32.3]]), _rs(["avg"], [[32.5]]))


def test_match_normalizes_int_float_and_strings():
    assert scorers.result_set_match(_rs(["n"], [[10]]), _rs(["n"], [[10.0]]))
    assert scorers.result_set_match(_rs(["s"], [[" x "]]), _rs(["s"], [["x"]]))
    assert scorers.result_set_match(_rs(["v"], [[None]]), _rs(["v"], [[None]]))


def test_mismatched_results_do_not_match():
    a = _rs(["gender", "n"], [["F", 10], ["M", 12]])
    assert not scorers.result_set_match(a, _rs(["gender", "n"], [["F", 10]]))  # missing row
    assert not scorers.result_set_match(a, _rs(["gender", "n"], [["F", 10], ["M", 13]]))
    assert not scorers.result_set_match(None, a)
    assert not scorers.result_set_match(a, None)
    # duplicate-row multiplicity matters (multiset, not set)
    assert not scorers.result_set_match(
        _rs(["n"], [[1], [1]]), _rs(["n"], [[1]])
    )


def test_execution_ok():
    assert scorers.execution_ok(_rs(["n"], []))
    assert not scorers.execution_ok(None)
    assert not scorers.execution_ok({"error": "boom"})
