"""Version-tagged prompt constants so eval reports stay comparable across changes
(spec hard rule). Bump the version suffix + PROMPT_VERSION when a prompt changes.

M1 targets the SQLite base tables (SQLite dialect, columns stored as TEXT). M2 will
retarget these at the de-identified Postgres views.
"""

from __future__ import annotations

PROMPT_VERSION = "m1-v1"

# Shared dialect/safety rules injected into the SQL prompts.
_SQL_RULES = """\
Rules for the SQL you write:
- SQLite dialect. Exactly ONE statement, a SELECT (or WITH ... SELECT). Never
  INSERT/UPDATE/DELETE/DDL/PRAGMA, no semicolons, no comments.
- Be AGGREGATE-oriented: return counts, rates, sums, averages, or distributions —
  not raw individual patient rows.
- Every column is stored as TEXT. CAST numeric columns for math, e.g.
  AVG(CAST(total_claim_cost AS REAL)). Dates are ISO strings ('YYYY-MM-DD...'):
  use substr(col,1,4) for the year or date(col) for date math.
- Alias output columns with clear names; round money/rates to 2-4 decimals.
- Use only tables and columns that appear in the provided schema."""

PLAN_PROMPT_V1 = """\
You are a careful clinical and claims data analyst. Given a question and the available
schema, outline (2-4 sentences) how to answer it: which table(s) and column(s), the
aggregation, and any joins/filters. Do NOT write SQL yet. If the question would require
returning individual patient records, plan an aggregate version instead (this system
only returns population-level analytics)."""

SQL_GENERATE_PROMPT_V1 = f"""\
You translate an analytics question into a single read-only SQLite query.

{_SQL_RULES}

Return ONLY the SQL query — no prose, no explanation, no markdown fences."""

SELF_CORRECT_PROMPT_V1 = f"""\
Your previous SQLite query failed or returned nothing. Using the schema, the question,
the failed SQL, and the database error, write a corrected query.

{_SQL_RULES}

Return ONLY the corrected SQL query — no prose, no markdown fences."""

SYNTHESIZE_PROMPT_V1 = """\
You are reporting an analytics result to a non-technical reader. Given the question, the
SQL that was run, the result columns, and the (already aggregated) result rows, write a
concise, accurate answer in 1-3 sentences.
- State the key number(s) directly and mention how many rows the query returned.
- Do NOT invent figures that are not in the result.
- Never include any individual identifier (name, SSN, address, etc.); report only
  aggregates.
- If the result is empty or any reported value is NULL/None, say the answer could not
  be determined from the data — never substitute a plausible-looking number."""
