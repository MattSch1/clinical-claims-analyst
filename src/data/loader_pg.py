"""Load the cached Synthea CSVs into Postgres base tables as the analytics OWNER.

Mirrors the SQLite loader (every column TEXT — codes must never be numified; views.sql
does all typing via ::date/::timestamptz/::int/::numeric). The ONE difference: empty CSV
fields map to NULL, because Synthea leaves STOP/DEATHDATE blank and ''::date /
''::timestamptz raise inside the de-identified views (SQLite tolerated blank strings).

COPY (not INSERT) for speed — observations alone is ~139k rows. NEVER connects as
analyst_ro (read-only on views); always the owner.
"""

from __future__ import annotations

import csv
from pathlib import Path

import psycopg
from psycopg import sql

from src.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV_DIR = REPO_ROOT / "data" / "synthea_out" / "csv"

# The eight base tables the de-identified views (views.sql) build on.
BASE_TABLES = (
    "patients", "encounters", "conditions", "procedures",
    "medications", "observations", "immunizations", "payers",
)

# Cost/amount/count columns loaded as `double precision` so the views expose NUMERIC
# values and the eval gold SQL's AVG/SUM and `payer_coverage = 0` comparisons work
# without per-query casts. Everything else stays TEXT — codes (SNOMED/LOINC/RxNorm/CVX),
# ids, and dates must never be numified (views.sql casts dates via ::timestamptz/::date).
NUMERIC_COLUMNS: dict[str, frozenset[str]] = {
    "patients": frozenset({"healthcare_expenses", "healthcare_coverage", "income"}),
    "encounters": frozenset({"base_encounter_cost", "total_claim_cost", "payer_coverage"}),
    "procedures": frozenset({"base_cost"}),
    "medications": frozenset({"base_cost", "payer_coverage", "dispenses", "totalcost"}),
    "immunizations": frozenset({"base_cost"}),
    "payers": frozenset({
        "amount_covered", "amount_uncovered", "revenue", "covered_encounters",
        "uncovered_encounters", "covered_medications", "uncovered_medications",
        "covered_procedures", "uncovered_procedures", "covered_immunizations",
        "uncovered_immunizations", "unique_customers", "qols_avg", "member_months",
    }),
}

csv.field_size_limit(2**31 - 1)


def _col_type(table: str, column: str) -> str:
    return "double precision" if column in NUMERIC_COLUMNS.get(table, frozenset()) else "TEXT"


def _load_table(conn: psycopg.Connection, table: str, csv_path: Path) -> int:
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = [h.strip().lower() for h in next(reader)]
        ident = sql.Identifier
        cols_sql = sql.SQL(", ").join(
            sql.SQL("{} {}").format(ident(c), sql.SQL(_col_type(table, c))) for c in header
        )
        copy_cols = sql.SQL(", ").join(ident(c) for c in header)
        with conn.cursor() as cur:
            # CASCADE drops dependent v_* views too — sidesteps the views.sql re-run trap.
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident(table)))
            cur.execute(sql.SQL("CREATE TABLE {} ({})").format(ident(table), cols_sql))
            copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(ident(table), copy_cols)
            n = 0
            width = len(header)
            with cur.copy(copy_stmt) as cp:
                for row in reader:
                    if len(row) < width:
                        row = row + [""] * (width - len(row))
                    elif len(row) > width:
                        row = row[:width]
                    # '' -> None so it lands as NULL (write_row maps None -> \N).
                    cp.write_row([None if v == "" else v for v in row])
                    n += 1
    return n


def load_postgres(csv_dir: str | Path | None = None, dsn: str | None = None) -> dict[str, int]:
    settings = get_settings()
    dsn = dsn or settings.analytics_owner_dsn
    if not dsn:
        raise SystemExit("ANALYTICS_OWNER_DSN is not set; cannot load Postgres.")
    csv_dir = Path(csv_dir or DEFAULT_CSV_DIR)

    counts: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        for table in BASE_TABLES:
            path = csv_dir / f"{table}.csv"
            if not path.exists():
                raise SystemExit(
                    f"missing CSV {path}; run `python -m src.data.load` first to generate it"
                )
            counts[table] = _load_table(conn, table, path)
            print(f"[loader_pg] {table}: {counts[table]} rows")
        conn.commit()
    return counts


if __name__ == "__main__":
    total = load_postgres()
    print(f"[loader_pg] done: {sum(total.values())} rows across {len(total)} base tables")
