"""Contract tests for chat completions through the public HTTP API."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.schemas import AppResponse


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def test_chat_completion_calls_configured_client_with_access_context(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "tenant": "ACME",
            "system": "concise",
            "prompt": "What is HarborRAG?",
        },
    )

    assert response.status_code == 200
    assert response.json()["message"] == {
        "role": "assistant",
        "content": "Harbor response",
    }
    assert response.json()["usage"]["total_tokens"] == 7
    assert response.json()["citations"] == [
        {"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}
    ]
    call = service.chat_calls[0]
    assert call["tenant_id"] == "ACME"
    assert call["principal_id"] == "dev"
    assert call["system"] == "concise"
    assert call["query"] == "What is HarborRAG?"


def test_chat_completion_defaults_tenant_and_system(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post("/v1/chat/completions", json={"prompt": "Hello"})

    assert response.status_code == 200
    call = service.chat_calls[0]
    assert call["tenant_id"] == "DEFAULT"
    assert call["system"] == "default"


def test_chat_completion_rejects_provider_specific_parameters(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "prompt": "Hello",
            "model": "must-not-be-accepted",
            "api_key": "must-not-be-accepted",
            "base_url": "https://provider.example",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_chat_completion_rejects_empty_prompt(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json={"prompt": ""})

    assert response.status_code == 422


def test_chat_failure_does_not_expose_provider_details(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def fail(*args, **kwargs):
        del args, kwargs
        return AppResponse(False, error="provider key and private endpoint")

    monkeypatch.setattr(service, "chat_completion", fail)
    response = client.post("/v1/chat/completions", json={"prompt": "Hello"})

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Chat service is unavailable"
    assert "private endpoint" not in response.text
