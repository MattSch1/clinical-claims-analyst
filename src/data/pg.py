"""Postgres read path — the agent's ONLY data access in M2.

Connects exclusively as analyst_ro (SELECT on the v_* views only; the role also has
default_transaction_read_only=on and statement_timeout=10s as DB-level backstops). Same
result shape as db.run_select so the agent tooling is backend-agnostic.
"""

from __future__ import annotations

import psycopg

from src.config import get_settings
from src.data.db import DEFAULT_ROW_CAP, VIEW_ALLOWLIST


def _ro_dsn() -> str:
    dsn = get_settings().analyst_ro_dsn
    if not dsn:
        raise RuntimeError("ANALYST_RO_DSN is not set; cannot reach the de-identified views.")
    return dsn


def run_select(sql: str, *, row_cap: int = DEFAULT_ROW_CAP) -> dict:
    """Run already-guarded SQL as analyst_ro. Returns {columns, rows, row_count,
    truncated}. Lets psycopg errors propagate (the caller maps them to {error: ...})."""
    with psycopg.connect(_ro_dsn()) as conn, conn.cursor() as cur:
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
    with psycopg.connect(_ro_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (view,),
        )
        return [r[0] for r in cur.fetchall()]


def distinct_values(view: str, column: str, *, limit: int = 40) -> list[str]:
    """Distinct values of a view column, to ground the model in real filter values."""
    if view not in VIEW_ALLOWLIST or not column.isidentifier():
        return []
    # view/column are validated against the allow-list / identifier rule above.
    query = (
        f'SELECT DISTINCT "{column}" FROM "{view}" '
        f'WHERE "{column}" IS NOT NULL ORDER BY 1 LIMIT {int(limit)}'
    )
    with psycopg.connect(_ro_dsn()) as conn, conn.cursor() as cur:
        cur.execute(query)
        return [str(r[0]) for r in cur.fetchall()]
