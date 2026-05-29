"""Append-only access audit log — implemented in M2.

Every sql_execute writes an immutable record: timestamp, request_id, SQL,
tables/columns touched, row count, outcome. Never updated or deleted.
Not implemented in M0.
"""
