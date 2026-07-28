"""HTTP ingestion routes delegate to the transport-neutral app service."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """Build an API client backed by the deterministic application test double."""

    monkeypatch.setattr(
        api_app,
        "select_app_service",
        lambda: (MockAppService(), "test"),
    )
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def test_start_ingestion_submits_the_temporal_shape(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ingestions",
        json={
            "tenant_id": "tenant-1",
            "connector_name": "local-docs",
            "run_id": "run-1",
            "max_artifacts": 3,
        },
    )

    assert response.status_code == 202
    assert response.json()["run"]["run_id"] == "run-1"
    assert response.json()["workflow"]["workflow_id"] == "mock-workflow"


def test_waiting_start_returns_the_completed_response(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ingestions",
        json={
            "tenant_id": "tenant-1",
            "connector_name": "local-docs",
            "wait": True,
        },
    )

    assert response.status_code == 200


def test_ingestion_status_and_result_are_available(client: TestClient) -> None:
    status = client.get("/api/v1/ingestions/run-1")
    result = client.get("/api/v1/ingestions/run-1/result")

    assert status.status_code == 200
    assert status.json()["status"]["run_id"] == "run-1"
    assert result.status_code == 200
    assert result.json()["result"]["status"] == "completed"


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_ingestion_control_actions_are_forwarded(
    client: TestClient,
    action: str,
) -> None:
    response = client.post(
        "/api/v1/ingestions/run-1/actions",
        json={"action": action},
    )

    assert response.status_code == 200
    assert response.json()["action"] == action


def test_retry_requires_artifact_ids(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ingestions/run-1/actions",
        json={"action": "retry"},
        headers={"X-Request-Id": "retry-validation"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"
    assert response.json()["error"]["trace_id"] == "retry-validation"
