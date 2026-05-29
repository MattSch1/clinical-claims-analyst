"""One-shot: prepare a (managed/cloud) Postgres for the agent — idempotent.

Run from a machine with Java (for Synthea), pointing the env at the managed DB:

    ANALYTICS_OWNER_DSN='postgresql://user:pw@host:5432/db' \
    ANALYST_RO_PASSWORD='...' AUDIT_WRITER_PASSWORD='...' \
      python scripts/bootstrap_cloud.py

It (1) generates the synthetic Synthea CSVs if they're not already present, then (2) loads
the base tables, applies the de-identified views (views.sql), and creates the analyst_ro
(views-only) + audit_writer (INSERT-only) roles and the append-only access_audit — exactly
the same `src.data.bootstrap_pg` flow used locally, just against the cloud DSN.

Synthetic data only. Set a STRONG, unique password for each role; never commit them.
"""

from __future__ import annotations

from src.config import get_settings
from src.data import bootstrap_pg, loader_pg
from src.data import load as synthea_load


def main() -> int:
    settings = get_settings()
    if not settings.analytics_owner_dsn:
        print("Set ANALYTICS_OWNER_DSN to the managed Postgres OWNER connection string.")
        return 2
    if not (settings.analyst_ro_password and settings.audit_writer_password):
        print("Set ANALYST_RO_PASSWORD and AUDIT_WRITER_PASSWORD (strong, unique).")
        return 2

    csv_dir = loader_pg.DEFAULT_CSV_DIR
    if not (csv_dir.is_dir() and any(csv_dir.glob("*.csv"))):
        print("No Synthea CSVs found — generating ~200 patients (needs Java 17+)…")
        synthea_load.main(["--patients", "200", "--seed", "12345"])

    host = settings.analytics_owner_dsn.split("@")[-1]  # host:port/db, no creds
    print(f"Bootstrapping de-identified views + roles + audit into {host} …")
    return bootstrap_pg.main()


if __name__ == "__main__":
    raise SystemExit(main())
