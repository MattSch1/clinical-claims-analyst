"""De-identification boundary (spec §5.4).

A single place every value can cross before it reaches a prompt or a response. A
regex/heuristic scrubber redacts anything identifier-shaped (SSN / phone / email /
long numeric IDs / IP / URL). On the SYNTHETIC, aggregate, view-only data this should
essentially never fire — proving the boundary exists and holds is the point.

The same high-precision patterns back the PHI-leakage scanner (`src/phi/leakage.py`),
which treats any hit as a hard failure rather than silently redacting.
"""

from __future__ import annotations

import re

# Ordered, high-precision identifier patterns. These are deliberately specific so they
# do NOT fire on legitimate aggregate output (counts, costs, rates, years).
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
    "ip": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "url": re.compile(r"\bhttps?://\S+"),
    # 9+ digit run that is NOT a decimal number (excludes costs like 11392137.10).
    "long_id": re.compile(r"(?<![\d.])\d{9,}(?![\d.])"),
}


def find(text: str) -> list[tuple[str, str]]:
    """Return (kind, matched_text) for every identifier-shaped span found."""
    if not text:
        return []
    hits: list[tuple[str, str]] = []
    for kind, pattern in PII_PATTERNS.items():
        hits.extend((kind, m.group(0)) for m in pattern.finditer(text))
    return hits


def scrub(text: str) -> str:
    """Redact identifier-shaped spans, e.g. '123-45-6789' -> '[REDACTED:ssn]'."""
    if not text:
        return text
    out = text
    for kind, pattern in PII_PATTERNS.items():
        out = pattern.sub(f"[REDACTED:{kind}]", out)
    return out


def is_clean(text: str) -> bool:
    return not find(text)
