"""Contract tests for retrieval through the public HTTP API."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_runtime.sdk import RetrievalLane


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def test_retrieval_search_uses_request_tenant_and_authenticated_principal(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/retrieval/vector",
        json={
            "query": "publication policy",
            "top_k": 3,
            "lane": "dense",
            "filters": {"category": "architecture"},
            "observe_graph": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["lane"] == "dense"
    assert len(payload["results"]) <= 3
    assert payload["results"][0]["content"] == "retrieved text"
    assert payload["results"][0]["metadata"] == {"category": "architecture"}
    assert "task_id" not in payload
    call = service.retrieval_calls[0]
    assert call["tenant_id"] == "DEFAULT"
    assert call["principal_id"] == "dev"
    assert call["filters"] == {"category": "architecture"}
    assert call["lane"] == RetrievalLane.DENSE
    assert call["observe_graph"] is False


def test_retrieval_transport_cannot_override_tenant(client: TestClient) -> None:
    response = client.post(
        "/v1/retrieval/vector",
        json={"query": "private", "filters": {"tenant_id": "another-tenant"}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


@pytest.mark.parametrize(
    "filters",
    [
        {f"field-{index}": index for index in range(33)},
        {"a": {"b": {"c": {"d": []}}}},
        {"field": "x" * 4_097},
    ],
)
def test_retrieval_filters_have_bounded_shape(
    client: TestClient,
    filters: dict[str, object],
) -> None:
    response = client.post(
        "/v1/retrieval/vector",
        json={"query": "publication", "filters": filters},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_retrieval_accepts_an_explicit_tenant(client: TestClient, service: MockAppService) -> None:
    response = client.post(
        "/v1/retrieval/vector",
        json={"query": "private", "tenant": "ACME"},
    )

    assert response.status_code == 200
    assert service.retrieval_calls[-1]["tenant_id"] == "ACME"
    assert service.retrieval_calls[-1]["filters"] == {}


def test_retrieval_filters_are_optional_in_openapi(client: TestClient) -> None:
    schema = client.get("/api/v1/openapi.json").json()["components"]["schemas"][
        "VectorSearchRequest"
    ]

    assert "filters" not in schema["required"]
    assert "filters" not in schema["examples"][0]


def test_retrieval_can_omit_content_and_metadata(client: TestClient) -> None:
    response = client.post(
        "/v1/retrieval/vector",
        json={
            "query": "publication policy",
            "include_content": False,
            "include_metadata": False,
        },
    )

    assert response.status_code == 200
    assert "content" not in response.json()["results"][0]
    assert "metadata" not in response.json()["results"][0]


def test_retrieval_failure_is_returned_without_provider_details(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def fail(*args, **kwargs):
        del args, kwargs
        return AppResponse(False, error="private provider URL")

    monkeypatch.setattr(service, "retrieve", fail)
    response = client.post(
        "/v1/retrieval/vector",
        json={"query": "publication"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Retrieval service is unavailable"
    assert "private provider URL" not in response.text


def test_vector_search_applies_score_threshold(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/retrieval/vector",
        json={"query": "publication", "score_threshold": 0.95},
    )

    assert response.status_code == 200
    assert response.json()["results"] == []
    assert service.retrieval_calls[-1]["score_threshold"] == 0.95


def test_legacy_vector_search_route_is_removed(client: TestClient) -> None:
    response = client.post("/v1/retrieval/search", json={"query": "publication"})

    assert response.status_code == 404
    assert "/v1/retrieval/search" not in client.get("/api/v1/openapi.json").json()["paths"]


def test_retrieval_search_is_post_only(client: TestClient) -> None:
    response = client.get(
        "/v1/retrieval/vector",
        params={"query": "must not enter URL logs"},
    )

    assert response.status_code == 405


def test_graph_triplet_search_uses_tenant_and_principal(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/retrieval/graph/triplets",
        json={
            "tenant": "ACME",
            "subject": "document:1",
            "predicate": "contains",
            "limit": 5,
        },
    )

    assert response.status_code == 200
    assert response.json()["triplets"][0]["predicate"]["relation_type"] == "contains"
    call = service.graph_retrieval_calls[-1]
    assert call["operation"] == "triplets"
    assert call["tenant_id"] == "ACME"
    assert call["principal_id"] == "dev"
    assert call["query"].limit == 5


def test_graph_triplet_search_requires_a_selector(client: TestClient) -> None:
    response = client.post("/v1/retrieval/graph/triplets", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/retrieval/graph/triplets", {"subject": "x" * 1_025}),
        (
            "/v1/retrieval/graph/paths",
            {"start_node": "x" * 1_025, "end_node": "document:2"},
        ),
        ("/v1/retrieval/graph/subgraphs", {"start_node": "x" * 1_025}),
        ("/v1/retrieval/graph/neighborhoods", {"query": "x" * 16_385}),
    ],
)
def test_graph_selectors_and_queries_have_length_limits(
    client: TestClient,
    path: str,
    payload: dict[str, str],
) -> None:
    response = client.post(path, json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_graph_path_search_exposes_typed_paths(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/retrieval/graph/paths",
        json={
            "start_node": "document:1",
            "end_node": "section:1",
            "relationship_types": ["contains"],
            "max_depth": 3,
            "direction": "outgoing",
        },
    )

    assert response.status_code == 200
    assert len(response.json()["paths"][0]["nodes"]) == 2
    query = service.graph_retrieval_calls[-1]["query"]
    assert query.max_depth == 3
    assert query.relationship_types[0].value == "contains"


def test_graph_subgraph_search_exposes_nodes_and_relations(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/retrieval/graph/subgraphs",
        json={
            "start_node": "document:1",
            "max_nodes": 25,
            "direction": "both",
        },
    )

    assert response.status_code == 200
    assert len(response.json()["nodes"]) == 2
    assert len(response.json()["relations"]) == 1
    assert service.graph_retrieval_calls[-1]["operation"] == "subgraph"


def test_unconfigured_graph_retrieval_is_a_capability_error(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def unavailable(*args, **kwargs):
        del args, kwargs
        return AppResponse(False, {"error_type": "HarborCapabilityError"})

    monkeypatch.setattr(service, "retrieve_graph_subgraph", unavailable)
    response = client.post(
        "/v1/retrieval/graph/subgraphs",
        json={"start_node": "document:1"},
    )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "harbor_capability_error"
