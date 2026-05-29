"""M0 smoke tests: config loads, package imports, FastAPI app constructs.

These are deliberately light — the real test suites (PHI controls, scorers,
graph transitions) arrive with their milestones (M2/M3).
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_settings_defaults_load():
    from src.config import get_settings

    s = get_settings()
    assert s.app_name == "clinical-claims-analyst"
    assert s.llm_model  # a model string is always configured
    assert s.langfuse_host.startswith("http")


def test_health_endpoint():
    from src.api.main import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


def test_ask_endpoint_returns_contract(monkeypatch):
    """/ask returns the M0 JSON contract. The model call is stubbed so the test is
    hermetic (no network) regardless of whether OPENAI_API_KEY is set."""
    from src.llm.client import LLMResult

    def _fake_complete(_messages, **_kwargs):
        return LLMResult(
            text="pong",
            model="stub-model",
            prompt_tokens=3,
            completion_tokens=1,
            total_tokens=4,
            cost_usd=0.0,
            latency_ms=1.2,
        )

    # Patch the symbol used inside the endpoint (main imports `client as llm`).
    monkeypatch.setattr("src.llm.client.complete", _fake_complete)

    from src.api.main import app

    client = TestClient(app)
    resp = client.post("/ask", json={"question": "What is this?"})
    assert resp.status_code == 200
    body = resp.json()
    # M0 response shape (subset of the full spec §11 contract).
    for key in ("answer", "sql", "row_count", "latency_ms", "model", "trace_url"):
        assert key in body, f"missing key: {key}"
    assert isinstance(body["answer"], str) and body["answer"]
