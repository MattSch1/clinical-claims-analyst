"""PHI-leakage scanner — implemented in M2.

Scans every model INPUT and OUTPUT for identifier patterns and asserts zero
leakage; any hit is a hard failure. Run in eval and CI. Not implemented in M0.
"""
