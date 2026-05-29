"""Read-only data access + a SELECT-only guard.

This is the code-level half of the "read-only, never mutate" hard rule. M1 runs
against the SQLite population opened in read-only mode (`?mode=ro`), with an
in-code guard that rejects anything that isn't a single SELECT/WITH. M2 adds the
database-level least-privilege role + de-identified-views-only enforcement on
Postgres; this guard stays as defense-in-depth.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from src.config import get_settings

# Statements/keywords that must never appear in an agent query.
_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "truncate", "attach", "detach", "pragma", "vacuum", "reindex", "grant",
    "revoke", "into", "merge",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b", re.IGNORECASE)
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

DEFAULT_ROW_CAP = 1000


class UnsafeQueryError(ValueError):
    """Raised when a query is not a single read-only SELECT."""


def assert_select_only(sql: str) -> str:
    """Validate that `sql` is exactly one read-only SELECT/WITH; return it cleaned.

    Raises UnsafeQueryError otherwise. This is intentionally strict (fail-closed).
    """
    if not sql or not sql.strip():
        raise UnsafeQueryError("empty query")
    stripped = _COMMENT_RE.sub(" ", sql).strip().rstrip(";").strip()
    if ";" in stripped:
        raise UnsafeQueryError("multiple statements are not allowed")
    lowered = stripped.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise UnsafeQueryError("only SELECT / WITH queries are allowed")
    hit = _FORBIDDEN_RE.search(stripped)
    if hit:
        raise UnsafeQueryError(f"forbidden keyword in query: {hit.group(1).lower()}")
    return stripped


def _connect_ro(path: str | None = None) -> sqlite3.Connection:
    """Open the SQLite DB in read-only mode (writes fail at the driver level)."""
    db_path = Path(path or get_settings().sqlite_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"SQLite database not found at {db_path}. Run `python -m src.data.load` first."
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run_select(sql: str, *, row_cap: int = DEFAULT_ROW_CAP, path: str | None = None) -> dict:
    """Run a guarded, read-only SELECT and return a structured result.

    Returns {columns, rows, row_count, truncated}. Raises UnsafeQueryError on a
    non-SELECT, or sqlite3.Error on a SQL/database error (caller handles both).
    """
    clean = assert_select_only(sql)
    conn = _connect_ro(path)
    try:
        cur = conn.execute(clean)
        columns = [d[0] for d in cur.description] if cur.description else []
        fetched = cur.fetchmany(row_cap + 1)
        truncated = len(fetched) > row_cap
        rows = [list(r) for r in fetched[:row_cap]]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    finally:
        conn.close()


def list_tables(path: str | None = None) -> list[str]:
    conn = _connect_ro(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def table_columns(table: str, path: str | None = None) -> list[str]:
    conn = _connect_ro(path)
    try:
        # table comes from sqlite_master (trusted), quoted defensively.
        safe = table.replace('"', '""')
        rows = conn.execute(f'PRAGMA table_info("{safe}")').fetchall()
        return [r[1] for r in rows]
    finally:
        conn.close()


def distinct_values(
    table: str, column: str, *, limit: int = 40, path: str | None = None
) -> list[str]:
    """Distinct non-empty values of a (trusted) column — used to ground the model in
    the actual category labels (e.g. encounterclass) rather than letting it guess."""
    conn = _connect_ro(path)
    try:
        safe_t = table.replace('"', '""')
        safe_c = column.replace('"', '""')
        rows = conn.execute(
            f'SELECT DISTINCT "{safe_c}" FROM "{safe_t}" '
            f"WHERE \"{safe_c}\" IS NOT NULL AND \"{safe_c}\" != '' "
            f"ORDER BY 1 LIMIT {int(limit)}"
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        conn.close()
