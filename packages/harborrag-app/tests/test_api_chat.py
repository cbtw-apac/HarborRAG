"""Contract tests for session-oriented chat through the public HTTP API."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_runtime.chat import ChatPrompt


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _session(client: TestClient, tenant: str = "DEFAULT") -> str:
    response = client.post("/v1/chat/sessions", json={"tenant": tenant})
    assert response.status_code == 201
    payload = response.json()
    assert payload["greeting"]
    assert payload["session_id"].startswith("session-")
    return payload["session_id"]


def test_chat_session_then_completion_uses_only_session_identity(
    client: TestClient,
    service: MockAppService,
) -> None:
    session_id = _session(client, "ACME")

    response = client.post(
        "/v1/chat/completions",
        json={
            "tenant": "ACME",
            "session_id": session_id,
            "prompt": "Explain HarborRAG",
            "graph_search": "true",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"]["content"] == "Harbor response"
    assert payload["session_id"] == session_id
    assert "user_id" not in payload
    assert response.headers["cache-control"] == "no-store"
    call = service.chat_calls[0]
    assert call["principal_id"] == "dev"
    assert call["session_id"] == session_id
    assert call["system"] is ChatPrompt.DEFAULT
    assert call["graph_search"] is True


def test_chat_completion_rejects_get_to_keep_prompt_out_of_url(client: TestClient) -> None:
    response = client.get(
        "/v1/chat/completions",
        params={"session_id": "session-1", "prompt": "sensitive"},
    )

    assert response.status_code == 405


def test_chat_completion_rejects_unknown_or_cross_tenant_session(client: TestClient) -> None:
    session_id = _session(client, "ACME")

    missing = client.post(
        "/v1/chat/completions",
        json={"tenant": "ACME", "session_id": "session-missing", "prompt": "Hello"},
    )
    wrong_tenant = client.post(
        "/v1/chat/completions",
        json={"tenant": "OTHER", "session_id": session_id, "prompt": "Hello"},
    )

    assert missing.status_code == 404
    assert wrong_tenant.status_code == 404


def test_chat_completion_requires_session_and_prompt(client: TestClient) -> None:
    no_session = client.post("/v1/chat/completions", json={"prompt": "Hello"})
    no_prompt = client.post(
        "/v1/chat/completions",
        json={"session_id": "session-1"},
    )

    assert no_session.status_code == 422
    assert no_prompt.status_code == 422


def test_chat_completion_rejects_provider_specific_query_parameters(client: TestClient) -> None:
    session_id = _session(client)

    response = client.post(
        "/v1/chat/completions",
        json={
            "session_id": session_id,
            "prompt": "Hello",
            "model": "provider-secret-model",
        },
    )

    assert response.status_code == 422
