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
    double = MockAppService()
    double.tenant_projects = {"ACME": {"cpm"}}
    double.catalog.add("cpm", "cpm/cpm-tickets", ("scope-cpm-tickets",), documents=2)
    return double


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _session(client: TestClient, tenant: str = "DEFAULT", project_id: str | None = None) -> str:
    body = {} if project_id is None else {"project_id": project_id}
    response = client.post("/v1/chat/sessions", json=body, headers={"X-Tenant-Id": tenant})
    assert response.status_code == 201
    payload = response.json()
    assert payload["session_id"].startswith("ses_")
    return payload["session_id"]


def test_a_completion_in_a_session_runs_as_the_sessions_owner_in_its_tenant(
    client: TestClient,
    service: MockAppService,
) -> None:
    session_id = _session(client, "ACME", "cpm")

    response = client.post(
        "/v1/chat/completions",
        json={"session_id": session_id, "message": "Explain HarborRAG"},
        headers={"X-Tenant-Id": "ACME"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"].startswith("Two candidates match")
    assert payload["session_id"] == session_id
    assert "user_id" not in payload
    assert response.headers["cache-control"] == "no-store"
    request = service.chat.requests[0]
    assert request.metadata.tenant_id == "ACME"
    # auth_mode=none: the implicit dev principal is also the end user.
    assert request.metadata.user_id == "DEFAULT_USER"
    assert request.metadata.conversation_id == session_id
    assert service.chat.prompts == [ChatPrompt.DEFAULT]
    assert service.retrieval.request.filters == {"source_scope_id": ["scope-cpm-tickets"]}  # type: ignore[union-attr]


def test_chat_completion_rejects_get_to_keep_the_message_out_of_the_url(
    client: TestClient,
) -> None:
    response = client.get(
        "/v1/chat/completions",
        params={"session_id": "ses_1", "message": "sensitive"},
    )

    assert response.status_code == 405


def test_chat_completion_rejects_unknown_or_cross_tenant_session(client: TestClient) -> None:
    session_id = _session(client, "ACME")

    missing = client.post(
        "/v1/chat/completions",
        json={"session_id": "ses_missing", "message": "Hello"},
        headers={"X-Tenant-Id": "ACME"},
    )
    wrong_tenant = client.post(
        "/v1/chat/completions",
        json={"session_id": session_id, "message": "Hello"},
        headers={"X-Tenant-Id": "OTHER"},
    )

    assert missing.status_code == 404
    assert wrong_tenant.status_code == 404


def test_chat_completion_requires_a_message(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json={"session_id": "ses_1"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "message"


def test_chat_completion_rejects_provider_specific_fields(client: TestClient) -> None:
    session_id = _session(client)

    response = client.post(
        "/v1/chat/completions",
        json={"session_id": session_id, "message": "Hello", "model": "provider-secret-model"},
    )

    assert response.status_code == 422


def test_chat_completion_rejects_a_malformed_project_id(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions", json={"message": "Hi", "project_ids": ["../etc"]}
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "project_ids.0"
