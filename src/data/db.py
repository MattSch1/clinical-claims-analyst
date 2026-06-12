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

# Statements/keywords that must never appear in an agent query. REPLACE is forbidden
# only as a statement keyword (REPLACE INTO / CREATE OR REPLACE) — `replace(col, …)`
# the string function is legitimate SQL, so it's exempted via a lookahead for "(".
_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create",
    "truncate", "attach", "detach", "pragma", "vacuum", "reindex", "grant",
    "revoke", "into", "merge",
)
_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(_FORBIDDEN) + r"|replace(?!\s*\())\b", re.IGNORECASE
)
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


# ── M2 views-only + aggregate-shape guards (defense-in-depth on the Postgres path) ──
# The PRIMARY control is the DB grant (analyst_ro can only SELECT the v_* views). These
# code guards give a fast, structured rejection before a round-trip and hold if the DB
# layer were ever misconfigured. assert_select_only above is left byte-for-byte intact.
VIEW_ALLOWLIST = frozenset({
    "v_patients", "v_encounters", "v_conditions", "v_procedures",
    "v_medications", "v_observations", "v_immunizations", "v_payers",
})
# Base tables + the per-patient offset table (reading offset_days would let an attacker
# reverse the date-shift and recover absolute service dates — the key re-id vector).
FORBIDDEN_RELATIONS = frozenset({
    "patients", "encounters", "conditions", "procedures", "medications",
    "observations", "immunizations", "payers", "careplans", "claims",
    "claims_transactions", "payer_transitions", "organizations", "providers",
    "allergies", "devices", "imaging_studies", "supplies", "patient_date_offset",
})
_AGG_OR_GROUP_RE = re.compile(
    r"\b(count|sum|avg|min|max)\s*\(|\bgroup\s+by\b|\bdistinct\b", re.IGNORECASE
)
_SELECT_STAR_RE = re.compile(r"select\s+\*", re.IGNORECASE)
# Postgres exposes information_schema / pg_catalog to PUBLIC; an agent query must never
# enumerate them (it could confirm a base-table 'ssn' column exists). The trusted
# schema_inspect metadata calls in src/data/pg.py do NOT go through this guard.
_METADATA_RE = re.compile(
    r"\b(information_schema|pg_catalog|pg_class|pg_attribute|pg_namespace|pg_tables|"
    r"pg_roles|pg_authid|pg_shadow|pg_stat\w*)\b",
    re.IGNORECASE,
)
# Surrogate per-individual keys: a result that exposes one of these as an OUTPUT column
# is a row-level individual record set (one row per patient/encounter), which the
# no-PHI-to-model regime forbids — even though DISTINCT/GROUP BY makes it "aggregate-shaped".
SURROGATE_OUTPUT_KEYS = frozenset({"patient", "encounter", "id"})

_IDENT = r'[A-Za-z_][A-Za-z0-9_]*'
# FROM/JOIN relation targets (subqueries start with "(" and are skipped; their inner
# FROMs are matched on the same pass). CTE names declared in WITH are also allowed.
_REL_TOKEN_RE = re.compile(
    rf'\b(?:from|join)\s+(?:lateral\s+)?("?{_IDENT}"?(?:\."?{_IDENT}"?)?)', re.IGNORECASE
)
_CTE_NAME_RE = re.compile(
    rf'(?:\bwith\s+(?:recursive\s+)?|,\s*)("?{_IDENT}"?)\s+as\s*\(', re.IGNORECASE
)
_STRING_LIT_RE = re.compile(r"'(?:[^']|'')*'")
_UUID_VALUE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _scrub_sql_text(sql: str) -> str:
    """Comments and string literals removed, so guards never match inside either."""
    return _STRING_LIT_RE.sub("''", _COMMENT_RE.sub(" ", sql))


def assert_views_only(sql: str) -> str:
    """Reject any reference to a base table or patient_date_offset (the v_* views use
    an underscore prefix, so \\bencounters\\b never matches inside v_encounters).

    Layered: a structural ALLOWLIST of FROM/JOIN targets (v_* views + the query's own
    CTEs — also blocks pg_catalog relations like pg_database/pg_proc that the blocklist
    misses) plus a relation-position blocklist scan as a second belt. The blocklist is
    anchored to FROM/JOIN so a column alias like `COUNT(...) AS patients` doesn't trip it."""
    cleaned = _scrub_sql_text(sql)
    if _METADATA_RE.search(cleaned):
        raise UnsafeQueryError(
            "system catalog / information_schema access is not allowed; query the v_* views"
        )
    for rel in FORBIDDEN_RELATIONS:
        if re.search(
            rf"\b(?:from|join)\s+(?:lateral\s+)?(?:\"?public\"?\.)?\"?{re.escape(rel)}\"?\b",
            cleaned,
            re.IGNORECASE,
        ):
            raise UnsafeQueryError(
                f"query references non-view relation '{rel}'; only the de-identified "
                "v_* views may be queried"
            )
    assert_relations_allowlisted(sql)
    return sql


def assert_relations_allowlisted(sql: str) -> str:
    """Every FROM/JOIN target must be an allowed v_* view or a CTE defined in the query
    itself — anything else (base table, pg_* catalog, set-returning function) is rejected.
    Fail-closed counterpart to the blocklist above. Comma-join lists (FROM a, b) are
    walked so every relation in the list is checked."""
    cleaned = _scrub_sql_text(sql)
    ctes = {m.group(1).strip('"').lower() for m in _CTE_NAME_RE.finditer(cleaned)}

    def _check(token: str) -> None:
        parts = [p.strip('"').lower() for p in token.strip().split(".")]
        if len(parts) > 1 and parts[0] != "public":
            raise UnsafeQueryError(
                f"relation '{token}' is outside the public schema; query the v_* views"
            )
        if parts[-1] not in VIEW_ALLOWLIST and parts[-1] not in ctes:
            raise UnsafeQueryError(
                f"relation '{token}' is not an allowed de-identified view; query only: "
                + ", ".join(sorted(VIEW_ALLOWLIST))
            )

    # IGNORECASE is essential: an uppercase `AS` alias would otherwise be consumed as the
    # relation's alias-of-an-alias, desyncing the comma walk and letting later comma-joined
    # base tables (e.g. `FROM v_patients AS a, encounters AS b`) skip the allowlist.
    comma_rel = re.compile(
        rf'\s*("?{_IDENT}"?(?:\."?{_IDENT}"?)?)(?:\s+(?:as\s+)?{_IDENT})?', re.IGNORECASE
    )
    alias_re = re.compile(rf"\s+(?:as\s+)?{_IDENT}", re.IGNORECASE)
    for m in _REL_TOKEN_RE.finditer(cleaned):
        _check(m.group(1))
        # Walk a comma-join list: FROM rel [alias], rel2 [alias2], …
        pos = m.end()
        alias = alias_re.match(cleaned, pos)
        if alias:
            pos = alias.end()
        while pos < len(cleaned) and cleaned[pos:].lstrip().startswith(","):
            pos += len(cleaned[pos:]) - len(cleaned[pos:].lstrip()) + 1
            nxt = comma_rel.match(cleaned, pos)
            if not nxt:
                break
            _check(nxt.group(1))
            pos = nxt.end()
    return sql


def assert_aggregate_shape(sql: str) -> str:
    """Refuse row-level queries (no-PHI-to-model regime, spec §5.3): reject bare
    SELECT * and any query lacking an aggregate / GROUP BY / DISTINCT. Tuned to accept
    aggregate-OR-GROUP-BY-OR-DISTINCT so legitimate analytic queries aren't rejected."""
    cleaned = _COMMENT_RE.sub(" ", sql)
    if _SELECT_STAR_RE.search(cleaned):
        raise UnsafeQueryError("row-level 'SELECT *' is not allowed; use an aggregate query")
    if not _AGG_OR_GROUP_RE.search(cleaned):
        raise UnsafeQueryError(
            "query is not aggregate-shaped (needs COUNT/SUM/AVG/MIN/MAX, GROUP BY, or "
            "DISTINCT); row-level result sets are restricted"
        )
    return sql


def assert_safe_output_columns(columns: list[str]) -> None:
    """Semantic no-row-level-records check (run on the *result* columns, post-execution).

    DISTINCT/GROUP BY on a surrogate key (e.g. `SELECT DISTINCT patient FROM v_encounters`)
    passes assert_aggregate_shape syntactically but returns one row per individual. Reject
    any result that exposes a raw surrogate key as an output column — population analytics
    never needs a per-record key in its output.
    """
    bad = sorted({c for c in columns if c.lower() in SURROGATE_OUTPUT_KEYS})
    if bad:
        raise UnsafeQueryError(
            f"result exposes row-level surrogate key column(s) {bad}; return population "
            "aggregates, not per-record keys"
        )


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    parts, depth, buf = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _outermost_clauses(sql: str, start_kw: str, end_kws: tuple[str, ...]) -> list[str]:
    """Every `start_kw … end_kw` clause at paren-depth 0 (so CTE bodies and subqueries,
    which sit inside parens, never match — but every UNION/INTERSECT branch does)."""
    depth, i, n, low = 0, 0, len(sql), sql.lower()
    start_re = re.compile(rf"\b{start_kw}\b")
    end_re = re.compile(r"\b(" + "|".join(end_kws) + r")\b")
    clauses: list[str] = []
    start = None
    while i < n:
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if start is not None and end_re.match(low, i):
                clauses.append(sql[start:i])
                start = None  # fall through: this token may open the next clause
            if start is None:
                m = start_re.match(low, i)
                if m:
                    start, i = m.end(), m.end()
                    continue
        i += 1
    if start is not None:
        clauses.append(sql[start:])
    return clauses


# A select-list item that is a bare surrogate-key column, however aliased:
# `patient`, `e.patient`, `patient AS p`, `patient p`.
_SURROGATE_ITEM_RE = re.compile(
    rf"^(?:{_IDENT}\.)?(patient|encounter|id)(?:\s+(?:as\s+)?{_IDENT})?$", re.IGNORECASE
)
# `DISTINCT ON (...)` de-duplicates to one row per distinct key combo — on a surrogate
# key that's one row per individual, the same leak as GROUP BY patient.
_DISTINCT_ON_RE = re.compile(r"^\s*distinct\s+on\s*\((?P<cols>[^)]*)\)", re.IGNORECASE)
# Individual-level keys: grouping/distinct on these always yields one row per person.
# `id` is intentionally NOT here for GROUP BY — v_payers.id is an org key (~10 rows), so
# `GROUP BY p.id, p.name` is a legitimate payer aggregate, not an enumeration.
_INDIVIDUAL_KEY_RE = re.compile(rf"(?:{_IDENT}\.)?(patient|encounter)", re.IGNORECASE)


def assert_no_surrogate_output(sql: str) -> str:
    """Structural pre-execution check: the OUTERMOST select list must not return a
    surrogate key under ANY alias (`SELECT patient AS p … GROUP BY patient` defeats the
    name-based output check), and the outermost GROUP BY / DISTINCT ON must not key on one
    (one output row per individual is a row-level record set even without the key column).
    Per-individual grouping INSIDE a subquery/CTE that the outer query aggregates away
    (e.g. avg cost per patient) is legitimate and still passes."""
    cleaned = _scrub_sql_text(sql)
    for select_list in _outermost_clauses(cleaned, "select", ("from",)):
        on = _DISTINCT_ON_RE.match(select_list)
        if on:
            for col in _split_top_level(on.group("cols")):
                if re.fullmatch(
                    rf"(?:{_IDENT}\.)?(patient|encounter|id)", col.strip(), re.IGNORECASE
                ):
                    raise UnsafeQueryError(
                        "DISTINCT ON a surrogate key (patient/encounter/id) returns one row "
                        "per individual; use a population aggregate"
                    )
        body = _DISTINCT_ON_RE.sub("", select_list)
        for item in _split_top_level(body):
            expr = re.sub(r"^\s*(distinct|all)\b", "", item.strip(), flags=re.IGNORECASE).strip()
            if _SURROGATE_ITEM_RE.match(expr):
                raise UnsafeQueryError(
                    "query returns a row-level surrogate key (patient/encounter/id) from its "
                    "outermost SELECT; return population aggregates, not per-record keys"
                )
    for group_by in _outermost_clauses(
        cleaned, r"group\s+by", ("order", "limit", "having", "offset", "window", "fetch",
                                 "for", "union", "intersect", "except", "select")
    ):
        for item in _split_top_level(group_by):
            if _INDIVIDUAL_KEY_RE.fullmatch(item.strip()):
                raise UnsafeQueryError(
                    "query groups its outermost result by a per-individual key "
                    "(patient/encounter), which yields one row per individual; aggregate "
                    "per-individual figures inside a subquery instead"
                )
    return sql


def assert_safe_output_values(columns: list[str], rows: list[list]) -> None:
    """Value-level backstop (post-execution): a result column whose values are UUIDs is
    an enumeration of record keys regardless of what the SQL named it (catches renames
    the structural check can't trace, e.g. a key laundered through a CTE alias)."""
    if len(rows) < 2:
        return  # a 1-row aggregate can't enumerate individuals
    for ci, col in enumerate(columns):
        vals = [r[ci] for r in rows[:25] if r[ci] is not None]
        uuid_n = sum(1 for v in vals if isinstance(v, str) and _UUID_VALUE_RE.match(v))
        # >=2 (not "all"): a single decoy row (e.g. via UNION) must not launder a key dump.
        # Legit aggregate output (codes, names, counts, dates) never contains UUID values.
        if uuid_n >= 2:
            raise UnsafeQueryError(
                f"result column '{col}' contains row-level record identifiers (UUID keys); "
                "return population aggregates, not per-record keys"
            )


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
