"""Append-only access audit log (spec §5.5).

Every sql_execute writes exactly one immutable record (request_id, SQL, tables/views
touched, row count, outcome). The agent's role is read-only and CANNOT write, so audit
rows are inserted over a SEPARATE INSERT-only `audit_writer` connection — a compromised
read connection cannot tamper with the log. Append-only is enforced two ways: by
privilege (writer has INSERT only) AND by a BEFORE UPDATE/DELETE trigger that raises.

On the SQLite test backend, record_access is a no-op so the unit tests stay hermetic.
"""

from __future__ import annotations

import logging
import threading

from src.config import get_settings

logger = logging.getLogger(__name__)

# DDL applied by src/data/bootstrap_pg.py (as the analytics owner). GENERATED ALWAYS AS
# IDENTITY means the INSERT-only writer never supplies the id.
ACCESS_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS access_audit (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    request_id  text,
    sql_text    text,
    tables      text[],
    row_count   integer,
    outcome     text NOT NULL CHECK (outcome IN ('ok', 'rejected', 'error')),
    error       text
);
"""

# Enforce append-only at the engine: any UPDATE/DELETE raises, and TRUNCATE (which a
# row-level trigger does NOT intercept) is blocked by a statement-level trigger.
ACCESS_AUDIT_GUARD_DDL = """
CREATE OR REPLACE FUNCTION access_audit_no_mutate() RETURNS trigger AS $fn$
BEGIN
    RAISE EXCEPTION 'access_audit is append-only (no UPDATE/DELETE/TRUNCATE allowed)';
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_access_audit_no_mutate ON access_audit;
CREATE TRIGGER trg_access_audit_no_mutate
    BEFORE UPDATE OR DELETE ON access_audit
    FOR EACH ROW EXECUTE FUNCTION access_audit_no_mutate();

DROP TRIGGER IF EXISTS trg_access_audit_no_truncate ON access_audit;
CREATE TRIGGER trg_access_audit_no_truncate
    BEFORE TRUNCATE ON access_audit
    FOR EACH STATEMENT EXECUTE FUNCTION access_audit_no_mutate();
"""

_INSERT = (
    "INSERT INTO access_audit (request_id, sql_text, tables, row_count, outcome, error) "
    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id"
)


def record_access(
    request_id: str | None,
    sql_text: str | None,
    tables: list[str],
    row_count: int | None,
    outcome: str,
    error: str | None = None,
) -> int | None:
    """Append one audit row over the INSERT-only writer connection. Returns the new
    row id, or None on the SQLite backend / if the write fails. NEVER raises — a failed
    audit write must not 500 the request (the caller logs it to state + Langfuse)."""
    settings = get_settings()
    if not settings.use_postgres:
        return None  # hermetic SQLite/test backend — audit is a Postgres-only control
    dsn = settings.audit_writer_dsn
    if not dsn:
        logger.warning("audit: AUDIT_WRITER_DSN unset; skipping audit write")
        return None
    try:
        with _writer_pool(dsn).connection() as conn, conn.cursor() as cur:
            cur.execute(_INSERT, (request_id, sql_text, tables, row_count, outcome, error))
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception:  # noqa: BLE001 - audit must never raise; the CALLER fails closed
        # (sql_execute refuses to return results when this comes back None).
        logger.warning("audit write failed", exc_info=True)
        return None


_writer_pools: dict = {}
_writer_lock = threading.Lock()


def _writer_pool(dsn: str):
    """Small pooled INSERT path (autocommit) for the audit writer."""
    from psycopg_pool import ConnectionPool

    with _writer_lock:
        pool = _writer_pools.get(dsn)
        if pool is None:
            pool = ConnectionPool(
                dsn,
                min_size=0,
                max_size=2,
                open=True,
                check=ConnectionPool.check_connection,
                kwargs={"autocommit": True},
                name="audit_writer",
            )
            _writer_pools[dsn] = pool
        return pool
