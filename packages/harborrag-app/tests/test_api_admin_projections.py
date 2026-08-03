"""Contract tests for safe tenant-scoped projection administration."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        api_app,
        "select_app_service",
        lambda: (MockAppService(), "test"),
    )
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def test_admin_can_inspect_tenant_projection_names(client: TestClient) -> None:
    response = client.get("/v1/admin/projections/DEFAULT")

    assert response.status_code == 200
    assert [item["physical_name"] for item in response.json()["vector_collections"]] == [
        "DEFAULT_routes",
        "DEFAULT_evidence",
    ]
    assert response.json()["graph_nodes"] == 12


def test_delete_requires_exact_tenant_confirmation(client: TestClient) -> None:
    response = client.delete(
        "/v1/admin/projections/DEFAULT",
        headers={"X-Confirm-Tenant": "ACME"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_delete_can_select_one_store_and_warns_that_reindex_is_required(
    client: TestClient,
) -> None:
    response = client.delete(
        "/v1/admin/projections/DEFAULT",
        headers={"X-Confirm-Tenant": "DEFAULT"},
        params=[("stores", "vector")],
    )

    assert response.status_code == 200
    assert response.json()["deleted_stores"] == ["vector"]
    assert response.json()["reindex_required"] is True
