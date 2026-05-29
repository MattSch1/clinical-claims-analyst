"""PHI-leakage scanner (spec §5.7) — a distinctive metric.

Scans every model INPUT and OUTPUT for identifier patterns and asserts ZERO leakage;
any hit is a hard failure (not a silent redaction — that's the scrubber's job). Run in
eval and CI: "zero PHI leakage across N runs, enforced in CI."

Two surfaces:
  * scan_text()  — high-precision identifier patterns (SSN/email/phone/IP/...), used on
                   model input/output. Won't fire on legitimate aggregate output.
  * scan_sql()   — flags references to direct-identifier COLUMN names (from the
                   classification map). Defense-in-depth: the views-only read-only role
                   already cannot reach those columns, but a query naming one is a smell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.phi import scrub
from src.phi.classification import all_identifier_columns

# Direct-identifier column names that are unambiguous enough to flag inside SQL without
# tripping on ordinary English (so we exclude bare words like "first"/"last"/"address").
_AMBIGUOUS = {"first", "last", "address", "city", "county", "state", "zip", "prefix", "suffix"}
_SQL_IDENTIFIER_COLUMNS = sorted(all_identifier_columns() - _AMBIGUOUS)
_SQL_COLUMN_RE = (
    re.compile(r"\b(" + "|".join(map(re.escape, _SQL_IDENTIFIER_COLUMNS)) + r")\b", re.IGNORECASE)
    if _SQL_IDENTIFIER_COLUMNS
    else None
)


@dataclass(frozen=True)
class Leak:
    kind: str          # pattern name (e.g. "ssn") or "identifier_column"
    match: str         # the offending substring
    where: str         # "input" | "output" | "sql" | caller-supplied label


class LeakageError(RuntimeError):
    """Raised when the PHI-leakage scanner finds a hit (hard failure)."""

    def __init__(self, leaks: list[Leak]):
        self.leaks = leaks
        preview = "; ".join(f"{leak.kind}@{leak.where}:{leak.match!r}" for leak in leaks[:5])
        super().__init__(f"PHI leakage detected ({len(leaks)} hit(s)): {preview}")


# The leakage GATE uses only HIGH-PRECISION identifier patterns. `long_id` (9+ digit
# runs) and `url` are deliberately EXCLUDED: de-identified clinical data is full of long
# numeric CODES (SNOMED/LOINC/RxNorm) that are NOT identifiers, so gating on them would
# false-positive constantly (e.g. SNOMED 314529007). scrub() still redacts them as a
# non-fatal belt-and-braces measure.
_GATE_KINDS: tuple[str, ...] = ("ssn", "email", "phone", "ip")


def scan_text(text: str, *, where: str = "text") -> list[Leak]:
    """High-precision identifier-pattern scan for model input/output (the hard gate)."""
    if not text:
        return []
    leaks: list[Leak] = []
    for kind in _GATE_KINDS:
        pattern = scrub.PII_PATTERNS[kind]
        leaks.extend(Leak(kind=kind, match=m.group(0), where=where) for m in pattern.finditer(text))
    return leaks


def scan_sql(sql: str) -> list[Leak]:
    """Flag direct-identifier column names appearing in a query (defense-in-depth)."""
    if not sql or _SQL_COLUMN_RE is None:
        return []
    return [
        Leak(kind="identifier_column", match=m.group(1), where="sql")
        for m in _SQL_COLUMN_RE.finditer(sql)
    ]


def assert_clean(text: str, *, where: str = "text") -> None:
    """Raise LeakageError if any identifier pattern is present (the hard gate)."""
    leaks = scan_text(text, where=where)
    if leaks:
        raise LeakageError(leaks)
