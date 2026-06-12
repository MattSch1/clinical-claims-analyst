"""DB-grant-level enforcement — the PRIMARY PHI control, verified against a live
Postgres when one is reachable (the local compose stack or CI's service container).

Skips cleanly when ANALYST_RO_DSN isn't set or the server is unreachable, so the
hermetic unit run stays green offline. This is the 'skip-if-no-PG integration check'
referenced by test_phi_controls.py.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")


def _ro_conn():
    dsn = os.getenv("ANALYST_RO_DSN")
    if not dsn:
        pytest.skip("ANALYST_RO_DSN not set — no live Postgres to verify grants against")
    try:
        return psycopg.connect(dsn, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


def test_analyst_ro_can_read_views():
    with _ro_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v_patients")
        assert cur.fetchone()[0] >= 0


@pytest.mark.parametrize("table", ["patients", "encounters", "patient_date_offset"])
def test_analyst_ro_cannot_read_base_tables(table):
    with _ro_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(f"SELECT * FROM {table} LIMIT 1")  # noqa: S608 - fixed test names


def test_analyst_ro_cannot_write():
    with _ro_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.Error):
            cur.execute("CREATE TABLE _should_fail (x int)")
