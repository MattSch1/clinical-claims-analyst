"""Generate (or load) a tiny synthetic Synthea population into SQLite.

M0 scope ONLY:
  * SQLite, base tables, lowercased column headers, every column stored as TEXT
    (codes MUST be TEXT — see src/phi/classification.py; numeric/date casts are
    deferred to the de-identified views at M2).
  * NO Postgres and NO views yet (that is M2).

Data is SYNTHETIC (MITRE Synthea). The repo is safe to make public.

Two modes:
  1. If CSVs already exist under --csv-dir, just (re)load them into SQLite.
  2. Otherwise, download the pinned Synthea jar (cached under .synthea/) and run
     it with Java to generate ~N patients as CSV, then load them.

Usage:
    python -m src.data.load                      # ~200 patients, default seed
    python -m src.data.load --patients 200 --seed 12345
    python -m src.data.load --csv-dir some/csv   # load pre-generated CSVs only
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

# Pinned Synthea release. `synthea-with-dependencies.jar` is a self-contained,
# freely redistributable synthetic-data generator (no PHI, no credentialing).
# v4.0.0 is the latest pinned stable release (carries the jar asset). Synthea
# needs JDK 17+ (MITRE recommends an LTS: 17 or 25).
SYNTHEA_VERSION = "v4.0.0"
SYNTHEA_JAR_URL = (
    "https://github.com/synthetichealth/synthea/releases/download/"
    f"{SYNTHEA_VERSION}/synthea-with-dependencies.jar"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
JAR_CACHE = REPO_ROOT / ".synthea" / "synthea-with-dependencies.jar"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "synthea_out"
DEFAULT_SQLITE = REPO_ROOT / "data" / "synthea.db"

# Increase CSV field size so very long clinical note/value fields don't blow up.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def _download_jar(dest: Path = JAR_CACHE) -> Path:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"[load] Using cached Synthea jar: {dest}")
        return dest
    if shutil.which("java") is None:
        raise SystemExit(
            "Java is required to generate a Synthea population but `java` was not "
            "found on PATH. Install a JDK (11+) or supply pre-generated CSVs via "
            "--csv-dir."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[load] Downloading Synthea {SYNTHEA_VERSION} jar -> {dest}")
    tmp = dest.with_suffix(".jar.part")
    urllib.request.urlretrieve(SYNTHEA_JAR_URL, tmp)  # noqa: S310 (trusted release URL)
    tmp.replace(dest)
    print(f"[load] Downloaded {dest.stat().st_size // (1024 * 1024)} MB")
    return dest


def run_synthea(patients: int, seed: int, out_dir: Path) -> Path:
    """Generate `patients` synthetic people as CSV. Returns the csv/ directory."""
    jar = _download_jar()
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "java",
        "-jar",
        str(jar),
        "-p",
        str(patients),
        "-s",
        str(seed),  # population seed (reproducible)
        "-cs",
        str(seed),  # clinician seed (reproducible)
        "--exporter.baseDirectory",
        str(out_dir),
        "--exporter.csv.export",
        "true",
        "--exporter.fhir.export",
        "false",  # CSV only — much faster, smaller
        "--exporter.hospital.fhir.export",
        "false",
        "--exporter.practitioner.fhir.export",
        "false",
        "--exporter.text.export",
        "false",
        "--exporter.ccda.export",
        "false",
        "--exporter.cpcds.export",
        "false",
        "--generate.only_alive_patients",
        "false",  # keep deceased patients so mortality/age analytics are possible
    ]
    print(f"[load] Generating {patients} synthetic patients (seed={seed})…")
    subprocess.run(cmd, check=True)  # noqa: S603 (fixed, trusted command)
    csv_dir = out_dir / "csv"
    if not csv_dir.is_dir():
        raise SystemExit(f"[load] Synthea did not produce a csv/ dir at {csv_dir}")
    return csv_dir


def _load_csv_into_sqlite(conn: sqlite3.Connection, csv_path: Path) -> int:
    """Load one CSV file into a table named after the file (lowercased).

    All columns are created as TEXT — deliberate: clinical codes must not be
    coerced to numbers, and typing is handled later in the de-identified views.
    """
    table = csv_path.stem.lower()
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            print(f"[load]   {csv_path.name}: empty, skipped")
            return 0
        cols = [c.strip().lower() for c in header]
        quoted = ", ".join(f'"{c}" TEXT' for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute(f'CREATE TABLE "{table}" ({quoted})')
        insert = f'INSERT INTO "{table}" VALUES ({placeholders})'
        n = 0
        batch: list[list[str]] = []
        for row in reader:
            # Tolerate ragged rows: pad/trim to header width.
            if len(row) < len(cols):
                row = row + [""] * (len(cols) - len(row))
            elif len(row) > len(cols):
                row = row[: len(cols)]
            batch.append(row)
            if len(batch) >= 1000:
                conn.executemany(insert, batch)
                n += len(batch)
                batch.clear()
        if batch:
            conn.executemany(insert, batch)
            n += len(batch)
    print(f"[load]   {table}: {n} rows")
    return n


def load_csv_dir_into_sqlite(csv_dir: Path, sqlite_path: Path) -> dict[str, int]:
    csv_files = sorted(csv_dir.glob("*.csv"))
    if not csv_files:
        raise SystemExit(f"[load] No CSV files found in {csv_dir}")
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    if sqlite_path.exists():
        sqlite_path.unlink()
    print(f"[load] Loading {len(csv_files)} tables into {sqlite_path}")
    counts: dict[str, int] = {}
    with sqlite3.connect(sqlite_path) as conn:
        for csv_path in csv_files:
            counts[csv_path.stem.lower()] = _load_csv_into_sqlite(conn, csv_path)
        conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load a tiny Synthea population into SQLite (M0).")
    ap.add_argument("--patients", type=int, default=200, help="number of patients to generate")
    ap.add_argument("--seed", type=int, default=12345, help="reproducible Synthea seed")
    ap.add_argument(
        "--csv-dir",
        type=Path,
        default=None,
        help="load pre-generated CSVs from here instead of running Synthea",
    )
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Synthea output dir")
    ap.add_argument("--sqlite-path", type=Path, default=DEFAULT_SQLITE, help="target SQLite file")
    args = ap.parse_args(argv)

    if args.csv_dir is not None:
        csv_dir = args.csv_dir
    else:
        existing = args.out_dir / "csv"
        csv_dir = existing if existing.is_dir() and any(existing.glob("*.csv")) else run_synthea(
            args.patients, args.seed, args.out_dir
        )

    counts = load_csv_dir_into_sqlite(csv_dir, args.sqlite_path)
    total = sum(counts.values())
    print(f"[load] Done: {len(counts)} tables, {total} total rows -> {args.sqlite_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
