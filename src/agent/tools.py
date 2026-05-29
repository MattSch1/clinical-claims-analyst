"""Agent tools (spec §8):

    schema_inspect()                 -> the schema the model may see.
    sql_execute(query, request_id)   -> guarded, read-only result | structured error.

M2 (Postgres backend): schema_inspect renders ONLY the de-identified v_* views (identifier
columns literally don't exist to the model), and sql_execute is the AUDIT ENVELOPE — it
runs the guard chain (SELECT-only → views-only → aggregate-shape), executes as the
read-only analyst_ro role, and writes exactly one append-only audit row for every outcome
(ok / rejected / error). The M1 SQLite path is kept for hermetic tests.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from functools import lru_cache

from src.config import get_settings
from src.data import db, pg
from src.phi import audit

logger = logging.getLogger(__name__)

# ── SQLite (M1 / test backend) ────────────────────────────────────────────────
_CORE_TABLES = (
    "patients", "encounters", "conditions", "medications", "procedures",
    "observations", "immunizations", "careplans", "claims", "claims_transactions",
    "payers", "payer_transitions", "organizations", "providers",
)
_HIDDEN_COLUMNS = {
    "ssn", "drivers", "passport", "first", "last", "maiden", "prefix", "suffix",
    "address", "lat", "lon", "birthplace", "phone",
}
_CATEGORICAL = (
    ("encounters", "encounterclass"), ("patients", "gender"), ("patients", "marital"),
    ("patients", "race"), ("patients", "ethnicity"), ("payers", "name"),
)

# ── Postgres (M2) — categorical grounding sourced from the views ────────────────
_PG_CATEGORICAL = (
    ("v_encounters", "encounterclass"), ("v_patients", "gender"), ("v_patients", "age_band"),
    ("v_patients", "marital"), ("v_patients", "race"), ("v_patients", "ethnicity"),
    ("v_payers", "name"),
)

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_LABEL_RE = re.compile(r"^\s*sql\s*:\s*", re.IGNORECASE)
_FIRST_STMT_RE = re.compile(r"(?is)\b(select|with)\b")
_VIEW_TOKEN_RE = re.compile(r"\bv_[a-z_]+\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def schema_inspect() -> str:
    """Render the schema the model may use. Cached (static for a given backend)."""
    if get_settings().use_postgres:
        try:
            return _schema_inspect_pg()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash a request
            logger.warning("schema_inspect (postgres) failed", exc_info=True)
            return f"(schema unavailable: {exc})"
    return _schema_inspect_sqlite()


def _schema_inspect_pg() -> str:
    lines = [
        "PostgreSQL de-identified views (PHI-safe — direct identifiers do not exist here).",
        "Query ONLY these v_* views:",
    ]
    for view in pg.list_views():
        cols = pg.view_columns(view)
        if cols:
            lines.append(f"- {view}({', '.join(cols)})")
    cat_lines = []
    for view, column in _PG_CATEGORICAL:
        try:
            vals = pg.distinct_values(view, column)
        except Exception:  # noqa: BLE001
            vals = []
        if vals:
            cat_lines.append(f"- {view}.{column}: {', '.join(vals)}")
    if cat_lines:
        lines.append("")
        lines.append("Exact values for key categorical columns (use these literally in filters):")
        lines.extend(cat_lines)
    return "\n".join(lines)


def _schema_inspect_sqlite() -> str:
    try:
        present = set(db.list_tables())
    except Exception as exc:  # noqa: BLE001
        return f"(schema unavailable: {exc})"
    lines = [
        "SQLite schema (all columns stored as TEXT — CAST for numeric/date math).",
        "Tables and columns available to you:",
    ]
    for table in _CORE_TABLES:
        if table not in present:
            continue
        cols = [c for c in db.table_columns(table) if c.lower() not in _HIDDEN_COLUMNS]
        if cols:
            lines.append(f"- {table}({', '.join(cols)})")
    cat_lines = []
    for table, column in _CATEGORICAL:
        if table not in present:
            continue
        try:
            vals = db.distinct_values(table, column)
        except Exception:  # noqa: BLE001
            vals = []
        if vals:
            cat_lines.append(f"- {table}.{column}: {', '.join(vals)}")
    if cat_lines:
        lines.append("")
        lines.append("Exact values for key categorical columns (use these literally in filters):")
        lines.extend(cat_lines)
    return "\n".join(lines)


def _view_names(sql: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _VIEW_TOKEN_RE.finditer(sql or "")})


def sql_execute(query: str, request_id: str | None = None) -> dict:
    """Run a query read-only and audited. Returns {columns, rows, row_count, truncated,
    audit_id} on success, or {error, outcome, audit_id} on a rejected/failed query.
    Never raises (callers branch on the 'error' key)."""
    if not get_settings().use_postgres:
        return _sqlite_execute(query)

    outcome, error, result = "ok", None, None
    tables = _view_names(query)
    try:
        clean = db.assert_select_only(query)
        db.assert_views_only(query)
        db.assert_aggregate_shape(query)
        result = pg.run_select(clean)
    except db.UnsafeQueryError as exc:
        outcome, error = "rejected", f"rejected: {exc}"
    except Exception as exc:  # noqa: BLE001 - DB/driver errors feed self-correction
        outcome, error = "error", f"{type(exc).__name__}: {exc}"

    audit_id = audit.record_access(
        request_id, query, tables, (result["row_count"] if result else None), outcome, error
    )
    if result is not None:
        result["audit_id"] = audit_id
        return result
    return {"error": error or "unknown error", "outcome": outcome, "audit_id": audit_id}


def _sqlite_execute(query: str) -> dict:
    try:
        return db.run_select(query)
    except db.UnsafeQueryError as exc:
        return {"error": f"rejected: {exc}"}
    except (sqlite3.Error, FileNotFoundError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def extract_sql(text: str) -> str:
    """Pull a single SQL statement out of an LLM response (handles ``` fences,
    a leading 'SQL:' label, trailing prose, and stray semicolons)."""
    t = (text or "").strip()
    fenced = _FENCE_RE.search(t)
    if fenced:
        t = fenced.group(1).strip()
    t = _LABEL_RE.sub("", t).strip()
    if not re.match(r"(?is)^\s*(select|with)\b", t):
        m = _FIRST_STMT_RE.search(t)
        if m:
            t = t[m.start():]
    return t.split(";")[0].strip()
