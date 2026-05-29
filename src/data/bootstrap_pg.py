"""Idempotent Postgres bootstrap — run as the analytics OWNER:

    python -m src.data.bootstrap_pg

Strict order:
  1. Load base tables (DROP CASCADE + CREATE TEXT + COPY) — cascade-dropping the old
     v_* views also sidesteps the no-CASCADE drop of patient_date_offset in views.sql.
  2. Apply src/data/views.sql verbatim as ONE multi-statement execute (NO params), so
     `SELECT setseed(0.42)` and `CREATE TABLE patient_date_offset AS ... random()` share
     one session and the per-patient date shifts are deterministic.
  3. Create/refresh the analyst_ro (views-only) and audit_writer (INSERT-only) roles,
     re-apply all grants/revokes (views are recreated each run, dropping old grants), and
     create the append-only access_audit table + its no-UPDATE/DELETE trigger.
  4. Smoke-SELECT v_patients / v_encounters to fail fast if a view didn't resolve.

Does NOT edit the starter views.sql.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql

from src.config import get_settings
from src.data import loader_pg
from src.data.db import VIEW_ALLOWLIST
from src.phi.audit import ACCESS_AUDIT_DDL, ACCESS_AUDIT_GUARD_DDL

VIEWS_SQL = Path(__file__).resolve().parent / "views.sql"


def _ensure_role(cur: psycopg.Cursor, role: str, password: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    verb = "ALTER" if cur.fetchone() else "CREATE"
    cur.execute(
        sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(
            sql.SQL(verb), sql.Identifier(role), sql.Literal(password)
        )
    )


def _apply_grants(cur: psycopg.Cursor, db_name: str) -> None:
    ro = sql.Identifier("analyst_ro")
    views = sql.SQL(", ").join(sql.Identifier(v) for v in sorted(VIEW_ALLOWLIST))
    forbidden = sql.SQL(", ").join(
        sql.Identifier(t) for t in (*loader_pg.BASE_TABLES, "patient_date_offset")
    )
    cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(ro))
    cur.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(ro))
    cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db_name), ro))
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ro))
    cur.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(views, ro))
    # Explicit belt-and-braces: base tables + the offset table are never readable.
    cur.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(forbidden, ro))
    cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '10s'").format(ro))
    cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ro))


def _apply_views(dsn: str) -> None:
    text = VIEWS_SQL.read_text(encoding="utf-8")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(text)  # multi-statement; works only with NO params
            except psycopg.Error as exc:
                raise SystemExit(f"views.sql failed to apply: {exc}") from exc
        conn.commit()


def _roles_grants_audit(dsn: str, ro_pw: str, aw_pw: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        db_name = conn.info.dbname
        _ensure_role(cur, "analyst_ro", ro_pw)
        _ensure_role(cur, "audit_writer", aw_pw)
        _apply_grants(cur, db_name)
        cur.execute(ACCESS_AUDIT_DDL)
        cur.execute(ACCESS_AUDIT_GUARD_DDL)
        aw = sql.Identifier("audit_writer")
        cur.execute(sql.SQL("GRANT INSERT ON access_audit TO {}").format(aw))
        # Column-level SELECT on id ONLY, so `INSERT ... RETURNING id` works while the
        # writer still cannot read the audit contents (request_id/sql_text/etc.).
        cur.execute(sql.SQL("GRANT SELECT (id) ON access_audit TO {}").format(aw))


def _smoke(dsn: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for view in ("v_patients", "v_encounters"):
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(view)))
            print(f"[bootstrap]   {view}: {cur.fetchone()[0]} rows")


def main() -> int:
    s = get_settings()
    owner_dsn = s.analytics_owner_dsn
    if not owner_dsn:
        raise SystemExit("ANALYTICS_OWNER_DSN is not set.")
    if not (s.analyst_ro_password and s.audit_writer_password):
        raise SystemExit("ANALYST_RO_PASSWORD and AUDIT_WRITER_PASSWORD must be set.")

    print("[bootstrap] 1/4 loading base tables (COPY, empty->NULL)…")
    loader_pg.load_postgres(dsn=owner_dsn)
    print("[bootstrap] 2/4 applying views.sql (one session; deterministic date-shift)…")
    _apply_views(owner_dsn)
    print("[bootstrap] 3/4 roles + grants + append-only audit…")
    _roles_grants_audit(owner_dsn, s.analyst_ro_password, s.audit_writer_password)
    print("[bootstrap] 4/4 smoke check…")
    _smoke(owner_dsn)
    print("[bootstrap] done ✓  (analyst_ro = views-only; audit_writer = INSERT-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
