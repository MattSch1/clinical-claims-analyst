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


def run_agent(question: str) -> AgentState:
    """Run the full agent loop for one question and return the final AgentState."""
    out = get_agent().invoke(
        AgentState(question=question), config={"recursion_limit": RECURSION_LIMIT}
    )
    return out if isinstance(out, AgentState) else AgentState.model_validate(out)
