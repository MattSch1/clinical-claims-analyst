"""LangGraph wiring of the six nodes (spec §8).

    START → plan → generate_sql → execute_sql → validate
                                                   │
                          ┌────────────────────────┴───────────┐
                     self_correct ──► execute_sql          synthesize → END
                     (loop, capped at MAX_RETRIES)

The ReAct-style control flow is explicit and inspectable (the whole point of using
LangGraph here rather than a hidden agent loop).
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.agent import nodes
from src.agent.state import AgentState
from src.phi import leakage

# A little headroom over the worst-case path (plan + MAX_RETRIES correction loops).
RECURSION_LIMIT = 30


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("plan", nodes.plan)
    g.add_node("generate_sql", nodes.generate_sql)
    g.add_node("execute_sql", nodes.execute_sql)
    g.add_node("validate", nodes.validate)
    g.add_node("self_correct", nodes.self_correct)
    g.add_node("synthesize", nodes.synthesize)

    g.add_edge(START, "plan")
    g.add_edge("plan", "generate_sql")
    g.add_edge("generate_sql", "execute_sql")
    g.add_edge("execute_sql", "validate")
    g.add_conditional_edges(
        "validate",
        nodes.route_after_validate,
        {"self_correct": "self_correct", "synthesize": "synthesize"},
    )
    g.add_edge("self_correct", "execute_sql")
    g.add_edge("synthesize", END)
    return g.compile()


_AGENT = None


def get_agent():
    """Compile the graph once and reuse it."""
    global _AGENT
    if _AGENT is None:
        _AGENT = build_graph()
    return _AGENT


def run_agent(question: str, request_id: str | None = None) -> AgentState:
    """Run the full agent loop for one question and return the final AgentState.

    `request_id` (e.g. the API's per-request id) ties the audit row + trace together; if
    omitted, AgentState mints one.
    """
    initial = (
        AgentState(question=question, request_id=request_id)
        if request_id
        else AgentState(question=question)
    )

    # Input PHI gate: a question carrying an identifier must never reach the model
    # (no-PHI-to-model regime). Refuse before invoking the graph — hard failure, no 500.
    in_leaks = leakage.scan_text(question, where="question")
    if in_leaks:
        initial.status = "refused"
        initial.leakage_hits = [f"input:{lk.kind}" for lk in in_leaks]
        initial.final_answer = (
            "That question appears to contain a personal identifier (e.g. SSN, phone, or "
            "email). For PHI safety I can't process it — please ask an aggregate, "
            "de-identified question."
        )
        initial.step_trace = ["input leakage gate: refused (PHI in question)"]
        return initial

    out = get_agent().invoke(initial, config={"recursion_limit": RECURSION_LIMIT})
    return out if isinstance(out, AgentState) else AgentState.model_validate(out)
