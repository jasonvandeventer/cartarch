"""/metrics is token-gated (issue #20).

Prometheus metrics leak internal state (request counts, error rates, active
users), so the route now requires an ``Authorization: Bearer <METRICS_TOKEN>``
header. These pin the three behaviours the issue's acceptance criteria name:
no/wrong token → 403, correct token → 200 + prometheus output, and that the
gate is scoped to /metrics only (/health stays public). The dependency reads
``METRICS_TOKEN`` from the environment at request time, so monkeypatch sets it
per-test without an app reimport.
"""

from __future__ import annotations

# Register the ORM models with Base.metadata at module import so the db_engine
# fixture's create_all() builds the schema (mirrors the other test modules, which
# import app.models at top level). Without this the file's first user-fixture
# test errors with "no such table: users" when run in isolation.
import app.models  # noqa: F401,E402


def test_metrics_without_token_forbidden(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    resp = client.get("/metrics")
    assert resp.status_code == 403


def test_metrics_wrong_token_forbidden(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    resp = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 403


def test_metrics_correct_token_ok(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    resp = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    # Prometheus exposition format — the HELP/TYPE preamble is always present.
    assert "# HELP" in resp.text


def test_metrics_scheme_is_case_insensitive(client, monkeypatch):
    """RFC 7235: the 'Bearer' scheme token is case-insensitive — a scraper
    sending lowercase still authenticates (the credential stays exact)."""
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    resp = client.get("/metrics", headers={"Authorization": "bearer s3cret"})
    assert resp.status_code == 200
    assert "# HELP" in resp.text


def test_metrics_unconfigured_fails_closed(client, monkeypatch):
    """No METRICS_TOKEN set → metrics stay private (403), never silently public."""
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    resp = client.get("/metrics", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 403


def test_health_remains_unauthenticated(client, monkeypatch):
    """The gate is scoped to /metrics — /health is still public."""
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
