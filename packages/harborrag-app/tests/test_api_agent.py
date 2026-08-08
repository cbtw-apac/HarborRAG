"""Contract tests for session-oriented agents through the public HTTP API."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _session(client: TestClient, tenant: str = "DEFAULT") -> str:
    response = client.post("/v1/agent/sessions", json={"tenant": tenant})
    assert response.status_code == 201
    assert response.json()["greeting"]
    return response.json()["session_id"]


def test_agent_session_then_completion_forwards_bounded_controls(
    client: TestClient,
    service: MockAppService,
) -> None:
    session_id = _session(client, "ACME")

    response = client.get(
        "/v1/agent/completions",
        params={
            "tenant": "ACME",
            "session_id": session_id,
            "prompt": "Connect the release policy to its owner.",
            "graph_search": "true",
            "max_steps": 3,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"]["content"] == "Agent response"
    assert payload["session_id"] == session_id
    assert "user_id" not in payload
    assert payload["tool_calls"] == [{"step": 1, "tool": "vector_search", "ok": True}]
    # The prompt travels in the query string (GET), so this response must
    # never be cached by an intermediary -- caching it would persist
    # sensitive prompt/answer content beyond this request's lifetime.
    assert response.headers["cache-control"] == "no-store"
    assert service.agent_calls[0]["principal_id"] == "dev"
    assert service.agent_calls[0]["graph_search"] is True


def test_agent_completion_rejects_unknown_session(client: TestClient) -> None:
    response = client.get(
        "/v1/agent/completions",
        params={"session_id": "session-missing", "prompt": "Hello"},
    )

    assert response.status_code == 404


def test_agent_completion_requires_session_and_prompt(client: TestClient) -> None:
    assert client.get("/v1/agent/completions", params={"prompt": "Hello"}).status_code == 422
    assert (
        client.get(
            "/v1/agent/completions",
            params={"session_id": "session-1"},
        ).status_code
        == 422
    )


def test_agent_run_resume_forwards_bounded_controls(
    client: TestClient,
    service: MockAppService,
) -> None:
    session_id = _session(client, "ACME")

    response = client.post(
        "/v1/agent/runs/run-1/resume",
        json={
            "tenant": "ACME",
            "session_id": session_id,
            "graph_search": True,
            "max_steps": 6,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run-1"
    assert payload["message"]["content"] == "Agent response"
    assert service.agent_resume_calls[0]["principal_id"] == "dev"
    assert service.agent_resume_calls[0]["graph_search"] is True
    assert service.agent_resume_calls[0]["max_steps"] == 6


def test_agent_run_resume_rejects_unknown_run(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/agent/runs/run-missing/resume",
        json={"tenant": "ACME", "session_id": "session-1"},
    )

    assert response.status_code == 404


def test_agent_run_resume_requires_session(client: TestClient) -> None:
    response = client.post("/v1/agent/runs/run-1/resume", json={"tenant": "ACME"})

    assert response.status_code == 422
