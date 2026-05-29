"""AgentState — the explicit, inspectable state the LangGraph agent threads through
its nodes (spec §8). One instance per `/ask` request.

M1 note: `schema_snapshot` is the SQLite **base-table** schema for now. M2 swaps the
data source to the de-identified Postgres views and adds the PHI controls; the state
shape does not change.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentState(BaseModel):
    # Inputs
    question: str
    request_id: str = Field(default_factory=lambda: uuid4().hex)  # attributes the audit row

    # Working memory
    schema_snapshot: str | None = None          # rendered view/table schema shown to the model
    plan: str | None = None                      # short analysis from the plan node
    code_hints: str | None = None                # exact codes resolved for named concepts
    candidate_sql: str | None = None             # current SQL to execute
    sql_attempts: list[str] = Field(default_factory=list)
    execution_result: dict[str, Any] | None = None  # {columns, rows, row_count, truncated}
    execution_error: str | None = None
    retry_count: int = 0                         # self-correction attempts (cap = MAX_RETRIES)
    route: str | None = None                     # validate node's branch decision

    # PHI controls (M2)
    audit_id: int | None = None                  # access_audit row id for this execution
    leakage_hits: list[str] = Field(default_factory=list)  # any model-I/O leakage findings

    # Outputs
    final_answer: str | None = None
    status: str = "pending"                      # ok | recovered | empty | failed | refused

    # Bookkeeping (cost/latency observability)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    step_trace: list[str] = Field(default_factory=list)

    @property
    def row_count(self) -> int:
        return int(self.execution_result.get("row_count", 0)) if self.execution_result else 0
