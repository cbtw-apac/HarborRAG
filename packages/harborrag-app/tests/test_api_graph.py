"""Contract tests for the graph screen API (overview + traverse)."""

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


def test_graph_overview_reports_projection_counts(client: TestClient) -> None:
    response = client.get("/v1/graph/overview", params={"tenant": "ACME"})

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "tenant": "ACME",
        "graph_name": "harborrag",
        "node_count": 12,
        "relation_count": 8,
    }


def test_graph_overview_defaults_to_default_tenant(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.get("/v1/graph/overview")

    assert response.status_code == 200
    assert response.json()["tenant"] == "DEFAULT"


def test_graph_traverse_uses_tenant_and_principal(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/graph/traverse",
        json={
            "tenant": "ACME",
            "start_node": "document:1",
            "max_nodes": 25,
            "direction": "both",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["nodes"]) == 2
    assert len(payload["relations"]) == 1
    call = service.graph_retrieval_calls[-1]
    assert call["operation"] == "subgraph"
    assert call["tenant_id"] == "ACME"
    assert call["principal_id"] == "dev"
    assert call["query"].start_node == "document:1"
    assert call["query"].max_nodes == 25


def test_graph_traverse_rejects_oversized_seed_node(client: TestClient) -> None:
    response = client.post(
        "/v1/graph/traverse",
        json={"start_node": "x" * 1_025},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_graph_traverse_rejects_duplicate_relationship_types(client: TestClient) -> None:
    response = client.post(
        "/v1/graph/traverse",
        json={"start_node": "document:1", "relationship_types": ["contains", "contains"]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_unconfigured_graph_traverse_is_a_capability_error(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def unavailable(*args, **kwargs):
        del args, kwargs
        return AppResponse(False, {"error_type": "HarborCapabilityError"})

    monkeypatch.setattr(service, "retrieve_graph_subgraph", unavailable)
    response = client.post(
        "/v1/graph/traverse",
        json={"start_node": "document:1"},
    )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "harbor_capability_error"


def test_graph_traverse_is_post_only(client: TestClient) -> None:
    response = client.get("/v1/graph/traverse", params={"start_node": "document:1"})

    assert response.status_code == 405


def test_list_graph_conflicts_returns_seeded_open_conflict(client: TestClient) -> None:
    response = client.get("/v1/graph/conflicts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["next_cursor"] is None
    assert len(payload["conflicts"]) == 1
    conflict = payload["conflicts"][0]
    assert conflict["id"] == "gc_1"
    assert conflict["status"] == "open"
    assert conflict["action"] is None


def test_resolve_graph_conflict_records_the_chosen_action(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post("/v1/graph/conflicts/gc_1/resolve", json={"action": "merge"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "resolved"
    assert payload["action"] == "merge"
    assert payload["resolved_by"] == "dev"
    call = service.graph_conflict_resolve_calls[-1]
    assert call["conflict_id"] == "gc_1"
    assert call["action"] == "merge"
    assert call["actor"] == "dev"


def test_resolve_graph_conflict_rejects_an_unknown_action(client: TestClient) -> None:
    response = client.post("/v1/graph/conflicts/gc_1/resolve", json={"action": "delete"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_resolve_unknown_graph_conflict_is_not_found(client: TestClient) -> None:
    response = client.post("/v1/graph/conflicts/does-not-exist/resolve", json={"action": "skip"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


def test_resolve_already_resolved_graph_conflict_is_a_conflict(client: TestClient) -> None:
    first = client.post("/v1/graph/conflicts/gc_1/resolve", json={"action": "skip"})
    assert first.status_code == 200

    second = client.post("/v1/graph/conflicts/gc_1/resolve", json={"action": "archive"})

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "harbor_conflict_error"
