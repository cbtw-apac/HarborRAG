"""Contract tests for reading and controlling ingestion runs."""

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


def test_task_status_is_connector_independent_and_temporal_free(client: TestClient) -> None:
    response = client.get("/v1/admin/ingestions/ing_1")

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "PROCESSING_DOCUMENTS"
    assert body["progress"]["processed"] == 1
    forbidden = {"workflow_id", "activity_id", "task_queue", "retry_attempt"}
    assert not (forbidden & set(body))
    assert not any(name in response.text for name in forbidden)


def test_metrics_labels_task_requests_by_route_template(client: TestClient) -> None:
    client.get("/v1/admin/ingestions/ing_1")

    metrics = client.app.state.api_metrics.render().decode()
    assert 'route="/v1/admin/ingestions/{ingestion_id}"' in metrics
    assert 'route="/v1/admin/ingestions/ing_1"' not in metrics


def test_document_results_support_status_and_cursor_parameters(client: TestClient) -> None:
    response = client.get(
        "/v1/admin/ingestions/ing_1/documents",
        params={"status": "SUCCESS", "cursor": "opaque", "limit": 25},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["source_item_id"] == "adr/0001.md"
    assert item["active_document_version_id"] == "document-version:1"


def test_cancellation_is_asynchronous_and_terminal_tasks_conflict(client: TestClient) -> None:
    accepted = client.post("/v1/admin/ingestions/ing_1/cancel")
    conflict = client.post("/v1/admin/ingestions/complete/cancel")

    assert accepted.status_code == 202
    assert accepted.json()["message"] == "Cancellation requested"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "invalid_state"


def test_pause_and_resume_are_asynchronous_and_terminal_tasks_conflict(
    client: TestClient,
) -> None:
    pause_accepted = client.post("/v1/admin/ingestions/ing_1/pause")
    pause_conflict = client.post("/v1/admin/ingestions/complete/pause")

    assert pause_accepted.status_code == 202
    assert pause_accepted.json()["message"] == "Pause requested"
    assert pause_conflict.status_code == 409
    assert pause_conflict.json()["error"]["code"] == "invalid_state"

    resume_accepted = client.post("/v1/admin/ingestions/ing_1/resume")
    resume_conflict = client.post("/v1/admin/ingestions/complete/resume")

    assert resume_accepted.status_code == 202
    assert resume_accepted.json()["message"] == "Resume requested"
    assert resume_conflict.status_code == 409
    assert resume_conflict.json()["error"]["code"] == "invalid_state"


def test_retry_failures_accepts_selected_or_all_documents(client: TestClient) -> None:
    selected = client.post(
        "/v1/admin/ingestions/ing_1/retry-failures",
        json={"document_ids": ["document:1"]},
    )
    all_failures = client.post("/v1/admin/ingestions/ing_1/retry-failures")

    assert selected.status_code == all_failures.status_code == 202
    assert selected.json()["accepted_document_count"] == 1
    assert UUID(selected.json()["retry_task_id"])


def test_retry_failures_rejects_an_oversized_document_id_list(client: TestClient) -> None:
    response = client.post(
        "/v1/admin/ingestions/ing_1/retry-failures",
        json={"document_ids": [f"document:{i}" for i in range(1001)]},
    )

    assert response.status_code == 422


def test_retry_failures_rejects_an_oversized_document_id(client: TestClient) -> None:
    response = client.post(
        "/v1/admin/ingestions/ing_1/retry-failures",
        json={"document_ids": ["x" * 256]},
    )

    assert response.status_code == 422
