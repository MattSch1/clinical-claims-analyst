"""Postgres read path — the agent's ONLY data access in M2.

Connects exclusively as analyst_ro (SELECT on the v_* views only; the role also has
default_transaction_read_only=on and statement_timeout=10s as DB-level backstops). Same
result shape as db.run_select so the agent tooling is backend-agnostic.
"""

from __future__ import annotations

import threading

from psycopg_pool import ConnectionPool

from src.config import get_settings
from src.data.db import DEFAULT_ROW_CAP, VIEW_ALLOWLIST

_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def _ro_dsn() -> str:
    dsn = get_settings().analyst_ro_dsn
    if not dsn:
        raise RuntimeError("ANALYST_RO_DSN is not set; cannot reach the de-identified views.")
    return dsn


def _pool(dsn: str) -> ConnectionPool:
    """One small, health-checked pool per DSN — managed-PG TLS handshakes are expensive,
    and check=check_connection discards connections the server idled out (Neon suspends)."""
    with _pools_lock:
        pool = _pools.get(dsn)
        if pool is None:
            pool = ConnectionPool(
                dsn,
                min_size=0,
                max_size=4,
                open=True,
                check=ConnectionPool.check_connection,
                name="analyst_ro",
            )
            _pools[dsn] = pool
        return pool


def _connection():
    return _pool(_ro_dsn()).connection()


def ping(timeout: float = 5.0) -> bool:
    """SELECT 1 over the analyst_ro path (used by /health). Raises on failure."""
    with _pool(_ro_dsn()).connection(timeout=timeout) as conn:
        conn.execute("SELECT 1")
    return True


def run_select(sql: str, *, row_cap: int = DEFAULT_ROW_CAP) -> dict:
    """Run already-guarded SQL as analyst_ro. Returns {columns, rows, row_count,
    truncated}. Lets psycopg errors propagate (the caller maps them to {error: ...})."""
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        columns = [d.name for d in cur.description] if cur.description else []
        fetched = cur.fetchmany(row_cap + 1)
        truncated = len(fetched) > row_cap
        rows = [list(r) for r in fetched[:row_cap]]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def list_views() -> list[str]:
    """The de-identified views the model is allowed to see (the allow-list)."""
    return sorted(VIEW_ALLOWLIST)


def view_columns(view: str) -> list[str]:
    if view not in VIEW_ALLOWLIST:
        return []
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (view,),
        )
        return [r[0] for r in cur.fetchall()]


# Clinical views with code+description columns, searched for concept→code resolution.
_CODED_VIEWS = ("v_conditions", "v_observations", "v_procedures", "v_medications")


def search_codes(words: list[str], *, per_view: int = 5) -> list[tuple[str, str, str, int]]:
    """Find (view, code, description, count) rows whose description contains ALL `words`
    (case-insensitive), most-frequent first, across the coded clinical views. Used to
    resolve a named concept ("type 2 diabetes") to its exact code without guessing or
    hardcoding. Word matching is parameterized (no SQL injection)."""
    if not words:
        return []
    where = " AND ".join("description ILIKE %s" for _ in words)
    params = [f"%{w}%" for w in words]
    out: list[tuple[str, str, str, int]] = []
    with _connection() as conn:
        for view in _CODED_VIEWS:
            if view not in VIEW_ALLOWLIST:
                continue
            query = (
                f'SELECT code, description, COUNT(*) AS n FROM "{view}" '
                f"WHERE {where} AND code IS NOT NULL "
                f"GROUP BY code, description ORDER BY n DESC LIMIT {int(per_view)}"
            )
            with conn.cursor() as cur:
                cur.execute(query, params)
                out.extend((view, r[0], r[1], int(r[2])) for r in cur.fetchall())
    return out


def distinct_values(view: str, column: str, *, limit: int = 40) -> list[str]:
    """Distinct values of a view column, to ground the model in real filter values."""
    if view not in VIEW_ALLOWLIST or not column.isidentifier():
        return []
    # view/column are validated against the allow-list / identifier rule above.
    query = (
        f'SELECT DISTINCT "{column}" FROM "{view}" '
        f'WHERE "{column}" IS NOT NULL ORDER BY 1 LIMIT {int(limit)}'
    )
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(query)
        return [str(r[0]) for r in cur.fetchall()]
