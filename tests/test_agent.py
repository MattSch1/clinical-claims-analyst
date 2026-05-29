"""Hermetic unit tests for the M1 agent's safety guard + SQL extraction (no LLM/DB)."""

from __future__ import annotations

import pytest

from src.agent.tools import extract_sql
from src.data.db import UnsafeQueryError, assert_select_only


def test_extract_sql_strips_fences_and_labels():
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("SQL: select count(*) from patients;").lower().startswith("select")
    out = extract_sql("Sure, here it is:\n\nSELECT a FROM t WHERE x=1")
    assert out == "SELECT a FROM t WHERE x=1"


def test_guard_allows_read_only_selects():
    assert assert_select_only("SELECT 1").lower().startswith("select")
    assert "with" in assert_select_only("WITH t AS (SELECT 1) SELECT * FROM t").lower()
    # trailing semicolon is tolerated (single statement)
    assert assert_select_only("SELECT 1;").lower().startswith("select")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "DROP TABLE patients",
        "UPDATE patients SET ssn='x'",
        "INSERT INTO t VALUES (1)",
        "DELETE FROM encounters",
        "SELECT 1; DELETE FROM patients",   # multiple statements
        "SELECT * INTO backup FROM patients",
        "PRAGMA table_info(patients)",
        "ATTACH DATABASE 'x' AS y",
    ],
)
def test_guard_rejects_writes_and_multistatement(bad):
    with pytest.raises(UnsafeQueryError):
        assert_select_only(bad)
