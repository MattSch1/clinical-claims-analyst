"""Hermetic PHI-control tests (no Postgres needed) — part of the definition-of-done.

Covers the M2 acceptance criteria that can be checked without a live DB: views-only
rejection, the row-level "SELECT *" refusal, the leakage gate, and the audit no-op on
the SQLite test backend. (Privilege-level enforcement is verified separately as a
skip-if-no-PG integration check.)
"""

from __future__ import annotations

import pytest

from src.data.db import UnsafeQueryError, assert_aggregate_shape, assert_views_only
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
