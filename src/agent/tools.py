"""Agent tools (spec §8):

    schema_inspect()      -> the schema text the model is allowed to see.
    sql_execute(query)    -> guarded, read-only result | structured error.

M1 runs against the SQLite base tables. To keep the prompt focused (and to start
honoring the PHI-safe ethos early) the patient table's direct-identifier columns are
omitted from the schema the model sees. This is a deliberately light touch — M2
replaces it with the real, classification-driven de-identified Postgres VIEWS
(`src/data/views.sql` + `src/phi/classification.py`) and a views-only DB role.
"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache

from src.data import db

# Core clinical + claims tables worth exposing for analytics (spec §4). Other
# generated Synthea tables exist but are noise for these questions.
_CORE_TABLES = (
    "patients", "encounters", "conditions", "medications", "procedures",
    "observations", "immunizations", "careplans", "claims", "claims_transactions",
    "payers", "payer_transitions", "organizations", "providers",
)

# Direct-identifier columns hidden from the model even in M1 (light touch; M2 does
# this properly via the classification map + de-identified views).
_HIDDEN_COLUMNS = {
    "ssn", "drivers", "passport", "first", "last", "maiden", "prefix", "suffix",
    "address", "lat", "lon", "birthplace", "phone",
}

# Low-cardinality columns whose exact values the model must know to filter correctly
# (otherwise it guesses, e.g. 'ED' instead of 'emergency'). Enumerated in the schema.
_CATEGORICAL = (
    ("encounters", "encounterclass"),
    ("patients", "gender"),
    ("patients", "marital"),
    ("patients", "race"),
    ("patients", "ethnicity"),
    ("payers", "name"),
)

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_LABEL_RE = re.compile(r"^\s*sql\s*:\s*", re.IGNORECASE)
_FIRST_STMT_RE = re.compile(r"(?is)\b(select|with)\b")


@lru_cache(maxsize=1)
def schema_inspect() -> str:
    """Render the schema the model may use. Cached (static for a given DB)."""
    try:
        present = set(db.list_tables())
    except Exception as exc:  # DB missing / unreadable — degrade, don't crash a request
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
        except Exception:  # noqa: BLE001 - a missing column shouldn't break schema_inspect
            vals = []
        if vals:
            cat_lines.append(f"- {table}.{column}: {', '.join(vals)}")
    if cat_lines:
        lines.append("")
        lines.append("Exact values for key categorical columns (use these literally in filters):")
        lines.extend(cat_lines)

    return "\n".join(lines)


def sql_execute(query: str) -> dict:
    """Run a query through the read-only SELECT-only guard.

    Returns {columns, rows, row_count, truncated} on success, or {error: "..."} on a
    rejected/failed query. Never raises (callers branch on the 'error' key).
    """
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
