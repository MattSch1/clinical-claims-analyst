"""De-identification boundary — implemented in M2.

A single code boundary every value crosses before it can reach a prompt or a
response; a regex/heuristic scrubber redacts anything identifier-shaped. On
synthetic data it should essentially never fire. Not implemented in M0.
"""
