"""Contract tests for the connector-independent public ingestion API."""

from __future__ import annotations

from uuid import UUID

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
    monkeypatch.setattr(
        api_app,
        "select_app_service",
        lambda: (service, "test"),
    )
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "connection_id",
    [
        "confluence-main",
        "jira-main",
        "harborrag-workspace",
    ],
)
def test_valid_connector_requests_return_accepted(
    client: TestClient,
    service: MockAppService,
    connection_id: str,
) -> None:
    response = client.post(
        "/v1/ingestions",
        json={"connection_id": connection_id},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    assert UUID(response.json()["task_id"])
    assert "workflow" not in response.json()
    assert service.submissions[-1].connection_id == connection_id
    assert service.submissions[-1].tenant_id == "DEFAULT"


def test_connection_id_is_required(client: TestClient) -> None:
    response = client.post("/v1/ingestions", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_unknown_request_fields_and_credentials_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/ingestions",
        json={
            "connection_id": "jira-main",
            "api_token": "must-not-enter-the-control-plane",
        },
    )

    assert response.status_code == 422
    assert "must-not-enter-the-control-plane" not in response.text


def test_connector_configuration_cannot_be_overridden_in_the_api(client: TestClient) -> None:
    response = client.post(
        "/v1/ingestions",
        json={
            "connection_id": "harborrag-workspace",
            "paths": ["another-root"],
            "include_attachments": False,
        },
    )

    assert response.status_code == 422


def test_repeated_idempotency_key_returns_the_same_task(client: TestClient) -> None:
    payload = {"connection_id": "harborrag-workspace"}
    first = client.post(
        "/v1/ingestions",
        json=payload,
        headers={"Idempotency-Key": "local-smoke-001"},
    )
    second = client.post(
        "/v1/ingestions",
        json=payload,
        headers={"Idempotency-Key": "local-smoke-001"},
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]


def test_tenant_and_force_mode_are_preserved_in_the_application_command(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/ingestions",
        json={
            "connection_id": "harborrag-workspace",
            "tenant": "ACME",
            "mode": "force",
        },
    )

    assert response.status_code == 202
    command = service.submissions[-1]
    assert command.tenant_id == "ACME"
    assert command.force_reprocess is True


def test_task_status_is_connector_independent_and_temporal_free(client: TestClient) -> None:
    response = client.get("/v1/ingestions/ing_1")

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "PROCESSING_DOCUMENTS"
    assert body["progress"]["processed"] == 1
    forbidden = {"workflow_id", "activity_id", "task_queue", "retry_attempt"}
    assert not (forbidden & set(body))
    assert not any(name in response.text for name in forbidden)


def test_metrics_labels_task_requests_by_route_template(client: TestClient) -> None:
    client.get("/v1/ingestions/ing_1")

    metrics = client.get("/api/v1/metrics").text
    assert 'route="/v1/ingestions/{task_id}"' in metrics
    assert 'route="/v1/ingestions/ing_1"' not in metrics


def test_document_results_support_status_and_cursor_parameters(client: TestClient) -> None:
    response = client.get(
        "/v1/ingestions/ing_1/documents",
        params={"status": "SUCCESS", "cursor": "opaque", "limit": 25},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["source_item_id"] == "adr/0001.md"
    assert item["active_document_version_id"] == "document-version:1"


def test_cancellation_is_asynchronous_and_terminal_tasks_conflict(client: TestClient) -> None:
    accepted = client.post("/v1/ingestions/ing_1/cancel")
    conflict = client.post("/v1/ingestions/complete/cancel")

    assert accepted.status_code == 202
    assert accepted.json()["message"] == "Cancellation requested"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "INGESTION_ALREADY_COMPLETED"


def test_retry_failures_accepts_selected_or_all_documents(client: TestClient) -> None:
    selected = client.post(
        "/v1/ingestions/ing_1/retry-failures",
        json={"document_ids": ["document:1"]},
    )
    all_failures = client.post("/v1/ingestions/ing_1/retry-failures")

    assert selected.status_code == all_failures.status_code == 202
    assert selected.json()["accepted_document_count"] == 1
    assert UUID(selected.json()["retry_task_id"])


def test_legacy_temporal_route_shape_is_not_public(client: TestClient) -> None:
    assert client.post("/api/v1/ingestions", json={}).status_code == 404
    assert client.get("/v1/ingestions/ing_1/result").status_code == 404
    assert client.post("/v1/ingestions/ing_1/actions", json={}).status_code == 404
