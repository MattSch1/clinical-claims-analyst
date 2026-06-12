"""API-surface tests: endpoint-level rate limiting (with proxy-header handling),
the body-size guard, and the /health contract."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.ratelimit import RateLimiter


def _client() -> TestClient:
    return TestClient(api_main.app)


def test_health_reports_database_state():
    resp = _client().get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "sqlite"  # conftest pins the hermetic backend


def test_ask_rate_limited_at_the_endpoint(monkeypatch):
    monkeypatch.setattr(api_main, "_limiter", RateLimiter(per_minute=1))
    client = _client()
    assert client.post("/ask", json={"question": "q1"}).status_code == 200
    resp = client.post("/ask", json={"question": "q2"})
    assert resp.status_code == 429
    assert "rate limit" in resp.json()["detail"]


def test_rate_limit_keys_on_first_xff_entry(monkeypatch):
    """Render sets the FIRST x-forwarded-for entry to the real client IP (a position it
    controls), so the limiter keys on it: the same client is limited regardless of which
    trailing proxy hops appear, and a different client IP is a separate bucket."""
    monkeypatch.setattr(api_main, "_limiter", RateLimiter(per_minute=1))
    client = _client()
    assert client.post(
        "/ask", json={"question": "q"}, headers={"x-forwarded-for": "1.1.1.1, 9.9.9.9"}
    ).status_code == 200
    # same client IP, different trailing hop → still the same bucket → limited
    assert client.post(
        "/ask", json={"question": "q"}, headers={"x-forwarded-for": "1.1.1.1, 8.8.8.8"}
    ).status_code == 429
    # a genuinely different client IP is its own bucket
    assert client.post(
        "/ask", json={"question": "q"}, headers={"x-forwarded-for": "2.2.2.2, 9.9.9.9"}
    ).status_code == 200


def test_cf_connecting_ip_takes_precedence(monkeypatch):
    monkeypatch.setattr(api_main, "_limiter", RateLimiter(per_minute=1))
    client = _client()
    hdr = {"cf-connecting-ip": "5.5.5.5", "x-forwarded-for": "1.1.1.1, 9.9.9.9"}
    assert client.post("/ask", json={"question": "q"}, headers=hdr).status_code == 200
    assert client.post("/ask", json={"question": "q"}, headers=hdr).status_code == 429


def test_oversized_body_is_rejected_before_parsing():
    resp = _client().post(
        "/ask",
        content=b'{"question": "' + b"x" * (api_main.MAX_BODY_BYTES + 1024) + b'"}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
