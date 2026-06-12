"""Hermetic PHI-control tests (no Postgres needed) — part of the definition-of-done.

Covers the M2 acceptance criteria that can be checked without a live DB: views-only
rejection, the row-level "SELECT *" refusal, the leakage gate, and the audit no-op on
the SQLite test backend. (Privilege-level enforcement is verified separately as a
skip-if-no-PG integration check.)
"""

from __future__ import annotations

import pytest

from src.data.db import (
    UnsafeQueryError,
    assert_aggregate_shape,
    assert_no_surrogate_output,
    assert_safe_output_columns,
    assert_safe_output_values,
    assert_select_only,
    assert_views_only,
)
from src.phi import audit, leakage


# ── views-only guard ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        "SELECT count(*) FROM patients",
        "SELECT count(*) FROM encounters",
        "SELECT * FROM patient_date_offset",
        "SELECT e.patient FROM v_encounters e JOIN patient_date_offset o ON e.patient = o.patient",
    ],
)
def test_views_only_rejects_base_and_offset(bad):
    with pytest.raises(UnsafeQueryError):
        assert_views_only(bad)


def test_views_only_accepts_view_query():
    assert assert_views_only("SELECT encounterclass, count(*) FROM v_encounters GROUP BY 1")


# ── aggregate-shape guard (the "SELECT * row-level attempt is refused" criterion) ──
def test_aggregate_shape_refuses_select_star():
    with pytest.raises(UnsafeQueryError):
        assert_aggregate_shape("SELECT * FROM v_patients")


def test_aggregate_shape_refuses_row_level():
    with pytest.raises(UnsafeQueryError):
        assert_aggregate_shape("SELECT age FROM v_patients")  # raw rows, no aggregate


def test_aggregate_shape_accepts_aggregates():
    assert assert_aggregate_shape("SELECT count(*) AS n FROM v_patients")
    assert assert_aggregate_shape("SELECT gender, count(*) FROM v_patients GROUP BY gender")
    assert assert_aggregate_shape("SELECT DISTINCT race FROM v_patients")


# ── semantic output-column guard (the surrogate-key row-level leak) ─────────────
def test_output_columns_reject_surrogate_keys():
    # `SELECT DISTINCT patient ...` is syntactically aggregate-shaped (the gap) ...
    assert assert_aggregate_shape("SELECT DISTINCT patient FROM v_encounters")
    # ... but the post-execution output-column check rejects the per-individual key.
    for cols in (["patient"], ["encounter", "n"], ["id"]):
        with pytest.raises(UnsafeQueryError):
            assert_safe_output_columns(cols)


def test_output_columns_allow_aggregates_and_categoricals():
    assert_safe_output_columns(["encounterclass", "n"])
    assert_safe_output_columns(["name", "covered"])
    assert_safe_output_columns(["avg_cost"])


# ── system-catalog enumeration is blocked on the agent path ─────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        "SELECT DISTINCT table_name FROM information_schema.columns",
        "SELECT relname FROM pg_catalog.pg_class",
        "SELECT count(*) FROM pg_tables",
        # not in the blocklist — caught by the relation ALLOWLIST
        "SELECT count(*) FROM pg_database",
        "SELECT proname, count(*) FROM pg_proc GROUP BY proname",
        "SELECT name, count(*) FROM pg_settings GROUP BY name",
    ],
)
def test_views_only_blocks_system_catalogs(bad):
    with pytest.raises(UnsafeQueryError):
        assert_views_only(bad)


def test_relation_allowlist_blocks_unknown_and_allows_ctes():
    # anything not a v_* view or the query's own CTE is rejected (fail-closed)
    with pytest.raises(UnsafeQueryError):
        assert_views_only("SELECT count(*) FROM generate_series(1, 10)")
    with pytest.raises(UnsafeQueryError):
        assert_views_only("SELECT count(*) FROM v_encounters e, some_table s")
    assert assert_views_only(
        "WITH inp AS (SELECT patient FROM v_encounters WHERE encounterclass = 'inpatient') "
        "SELECT count(*) FROM inp"
    )


def test_blocklist_anchored_to_relation_position():
    # a column alias named 'patients' must NOT trip the base-table blocklist
    assert assert_views_only(
        "SELECT p.name, COUNT(DISTINCT e.patient) AS patients FROM v_encounters e "
        "JOIN v_payers p ON e.payer = p.id GROUP BY p.name ORDER BY patients DESC"
    )


@pytest.mark.parametrize(
    "bad",
    [
        # uppercase-AS comma join: the 2nd+ relation must still hit the allowlist
        "SELECT count(*) FROM v_patients AS a, encounters AS b",
        "SELECT count(*) FROM v_patients AS a, patients AS b",
        "SELECT count(*) FROM v_patients a, v_encounters AS b, payers AS c",
        # mixed/lowercase forms too
        "SELECT count(*) FROM v_encounters e, some_table s",
    ],
)
def test_comma_join_allowlist_is_case_insensitive(bad):
    with pytest.raises(UnsafeQueryError):
        assert_views_only(bad)


def test_comma_join_allows_all_views_with_uppercase_as():
    assert assert_views_only(
        "SELECT count(*) FROM v_encounters AS e, v_payers AS p WHERE e.payer = p.id"
    )


# ── surrogate-key alias bypass (structural, pre-execution) ──────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        "SELECT patient AS p FROM v_encounters GROUP BY patient",       # the alias bypass
        "SELECT e.patient p FROM v_encounters e GROUP BY e.patient",    # bare alias
        "SELECT DISTINCT patient AS x FROM v_conditions",
        "SELECT count(*) FROM v_encounters GROUP BY patient",           # keyless row-level
        # a decoy UNION branch must not smuggle the enumeration past the guard
        "SELECT 'x' AS k FROM v_patients LIMIT 1 "
        "UNION ALL SELECT patient FROM v_encounters GROUP BY patient",
        # FETCH FIRST must not absorb the GROUP BY end (was a bypass)
        "SELECT count(*) FROM v_encounters GROUP BY patient FETCH FIRST 10 ROWS ONLY",
        # DISTINCT ON a surrogate key is one row per individual (was a bypass)
        "SELECT DISTINCT ON (patient) age_band, gender FROM v_patients",
        "SELECT DISTINCT ON (e.encounter) e.code FROM v_encounters e",
    ],
)
def test_surrogate_output_rejects_aliased_keys(bad):
    with pytest.raises(UnsafeQueryError):
        assert_no_surrogate_output(bad)


def test_surrogate_output_allows_inner_per_patient_aggregation():
    # per-individual grouping INSIDE a subquery/CTE that the outer query aggregates
    # away is the legitimate analytics shape (avg cost per patient, readmissions, …)
    assert assert_no_surrogate_output(
        "SELECT ROUND(AVG(pt_cost)::numeric, 2) FROM "
        "(SELECT patient, SUM(total_claim_cost) AS pt_cost FROM v_encounters "
        "GROUP BY patient) s"
    )
    assert assert_no_surrogate_output(
        "SELECT gender, count(*) FROM v_patients GROUP BY gender"
    )


def test_surrogate_output_allows_grouping_by_payer_id():
    # v_payers.id is an org-level key (~10 payers), NOT a per-individual surrogate —
    # grouping by it is the idiomatic payer aggregate and must pass.
    assert assert_no_surrogate_output(
        "SELECT p.name, COUNT(*) AS n FROM v_encounters e "
        "JOIN v_payers p ON e.payer = p.id GROUP BY p.id, p.name ORDER BY n DESC"
    )


def test_output_values_reject_uuid_enumeration():
    uuids = [
        ["0b1f4a52-1111-2222-3333-444455556666", 3],
        ["1c2e5b63-aaaa-bbbb-cccc-ddddeeeeffff", 1],
    ]
    with pytest.raises(UnsafeQueryError):
        assert_safe_output_values(["p", "n"], uuids)
    # one decoy row must not launder the dump
    with pytest.raises(UnsafeQueryError):
        assert_safe_output_values(["p"], [["x"], *[[r[0]] for r in uuids]])
    # legit aggregate output passes; 1-row results can't enumerate
    assert_safe_output_values(["encounterclass", "n"], [["inpatient", 12], ["er", 3]])
    assert_safe_output_values(["id_like"], [["0b1f4a52-1111-2222-3333-444455556666"]])


def test_replace_function_is_allowed_but_replace_statement_is_not():
    assert assert_select_only(
        "SELECT replace(description, 'x', 'y') AS d, count(*) FROM v_conditions GROUP BY 1"
    )
    with pytest.raises(UnsafeQueryError):
        assert_select_only("REPLACE INTO t VALUES (1)")


# ── leakage gate ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["SSN 123-45-6789", "call 555-123-4567", "mail x@y.com"])
def test_leakage_assert_clean_raises_on_pii(bad):
    with pytest.raises(leakage.LeakageError):
        leakage.assert_clean(bad)


def test_leakage_clean_on_aggregate_output():
    # No exception on legitimate aggregate prose.
    leakage.assert_clean("The 30-day readmission rate is 12.4% across 250 encounters in 2021.")


def test_scan_sql_flags_identifier_columns():
    assert leakage.scan_sql("SELECT ssn FROM patients")          # flagged
    assert not leakage.scan_sql("SELECT encounterclass, count(*) FROM v_encounters GROUP BY 1")


# ── audit no-op on the SQLite test backend ──────────────────────────────────────
def test_audit_no_op_on_sqlite():
    # conftest forces DATA_BACKEND=sqlite; record_access must be a no-op that never raises.
    assert audit.record_access("req-test", "SELECT 1", ["v_patients"], 1, "ok") is None


# ── audit hard rule fails CLOSED on the Postgres path ───────────────────────────
def test_sql_execute_fails_closed_when_audit_write_fails(monkeypatch):
    """No rows may leave sql_execute without an audit record (CLAUDE.md hard rule)."""
    from src.agent import tools
    from src.config import Settings

    pg_settings = Settings(DATA_BACKEND="postgres", ANALYST_RO_DSN="postgresql://x/x")
    monkeypatch.setattr("src.agent.tools.get_settings", lambda: pg_settings)
    monkeypatch.setattr(
        "src.data.pg.run_select",
        lambda sql, **kw: {
            "columns": ["encounterclass", "n"],
            "rows": [["inpatient", 5]],
            "row_count": 1,
            "truncated": False,
        },
    )
    monkeypatch.setattr("src.phi.audit.record_access", lambda *a, **kw: None)

    out = tools.sql_execute(
        "SELECT encounterclass, count(*) AS n FROM v_encounters GROUP BY 1", "req-1"
    )
    assert "error" in out and "audit" in out["error"]
    assert "rows" not in out


def test_sql_execute_returns_rows_when_audit_succeeds(monkeypatch):
    from src.agent import tools
    from src.config import Settings

    pg_settings = Settings(DATA_BACKEND="postgres", ANALYST_RO_DSN="postgresql://x/x")
    monkeypatch.setattr("src.agent.tools.get_settings", lambda: pg_settings)
    monkeypatch.setattr(
        "src.data.pg.run_select",
        lambda sql, **kw: {
            "columns": ["encounterclass", "n"],
            "rows": [["inpatient", 5]],
            "row_count": 1,
            "truncated": False,
        },
    )
    monkeypatch.setattr("src.phi.audit.record_access", lambda *a, **kw: 42)

    out = tools.sql_execute(
        "SELECT encounterclass, count(*) AS n FROM v_encounters GROUP BY 1", "req-1"
    )
    assert out["audit_id"] == 42 and out["rows"] == [["inpatient", 5]]


# ── leakage-flagged SQL must never execute ──────────────────────────────────────
def test_execute_sql_node_skips_pre_blocked_sql(monkeypatch):
    """_sql_update flags SQL referencing an identifier column and sets execution_error;
    execute_sql must then SKIP execution entirely (the old behavior ran it anyway)."""
    from src.agent import nodes
    from src.agent.state import AgentState

    def _boom(*a, **kw):
        raise AssertionError("flagged SQL was executed")

    monkeypatch.setattr("src.agent.tools.sql_execute", _boom)
    state = AgentState(
        question="q",
        candidate_sql="SELECT ssn FROM v_patients",
        execution_error="query references restricted identifier column(s): ssn",
        leakage_hits=["sql:ssn"],
        sql_leak_blocked=True,
    )
    out = nodes.execute_sql(state)
    assert out["execution_result"] is None
    assert any("BLOCKED" in s for s in out["step_trace"])


def test_execute_sql_does_not_block_on_stale_db_error(monkeypatch):
    """A stale DB error (sql_leak_blocked False) must NOT take the pre-block path — it
    would mislabel a real execution error as a policy rejection and never re-run."""
    from src.agent import nodes
    from src.agent.state import AgentState

    ran = {}

    def _fake_exec(sql, request_id=None):
        ran["sql"] = sql
        return {"columns": ["n"], "rows": [[1]], "row_count": 1, "truncated": False, "audit_id": 5}

    monkeypatch.setattr("src.agent.tools.sql_execute", _fake_exec)
    state = AgentState(
        question="q",
        candidate_sql="SELECT count(*) AS n FROM v_encounters GROUP BY 1",
        execution_error="SyntaxError: column foo does not exist",  # stale, from a prior try
        sql_leak_blocked=False,
    )
    out = nodes.execute_sql(state)
    assert ran.get("sql"), "stale-error SQL should be re-run, not blocked"
    assert out["execution_result"] is not None


def test_schema_inspect_does_not_cache_failures(monkeypatch):
    """One transient DB failure must degrade ONE request — not poison every later
    request until restart (the old lru_cache memoized the failure string)."""
    from src.agent import tools

    tools.clear_schema_cache()
    monkeypatch.setattr(
        tools, "_schema_inspect_sqlite", lambda: "(schema unavailable: transient)"
    )
    assert tools.schema_inspect().startswith("(schema unavailable")
    monkeypatch.setattr(tools, "_schema_inspect_sqlite", lambda: "real schema")
    assert tools.schema_inspect() == "real schema"  # retried, not poisoned
    tools.clear_schema_cache()
