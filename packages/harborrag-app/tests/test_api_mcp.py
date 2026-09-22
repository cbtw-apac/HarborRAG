"""Contract tests for the MCP telemetry API (ML4-P3)."""

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


def test_mcp_status_reports_reachable_and_healthy(client: TestClient) -> None:
    response = client.get("/v1/mcp/status")

    assert response.status_code == 200
    assert response.json() == {"reachable": True, "healthy": True}


def test_mcp_status_reports_unreachable_when_the_store_is_down(
    client: TestClient,
    service: MockAppService,
) -> None:
    service.mcp_healthy = False

    response = client.get("/v1/mcp/status")

    assert response.status_code == 200
    assert response.json() == {"reachable": False, "healthy": False}


def test_mcp_clients_reports_usage_by_client(client: TestClient) -> None:
    response = client.get("/v1/mcp/clients")

    assert response.status_code == 200
    payload = response.json()
    assert payload["clients"] == [
        {"client": "dev", "query_count": 3, "last_seen_at": "2026-09-17T12:00:00Z"}
    ]


def test_mcp_tools_reports_usage_by_tool(client: TestClient) -> None:
    response = client.get("/v1/mcp/tools")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tools"] == [
        {"tool": "retrieval_search", "call_count": 3, "avg_latency_ms": 42.5}
    ]


def test_mcp_queries_returns_matching_entries_for_a_valid_range(client: TestClient) -> None:
    response = client.get("/v1/mcp/queries", params={"range": "24h"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["range"] == "24h"
    assert payload["entries"] == [
        {
            "tool": "retrieval_search",
            "client": "dev",
            "latency_ms": 42,
            "created_at": "2026-09-17T12:00:00Z",
        }
    ]


def test_mcp_queries_defaults_to_24h(client: TestClient) -> None:
    response = client.get("/v1/mcp/queries")

    assert response.status_code == 200
    assert response.json()["range"] == "24h"


def test_mcp_queries_accepts_a_day_range_and_a_limit(client: TestClient) -> None:
    response = client.get("/v1/mcp/queries", params={"range": "7d", "limit": 1})

    assert response.status_code == 200
    assert response.json()["range"] == "7d"


def test_mcp_queries_rejects_an_invalid_range_format(client: TestClient) -> None:
    response = client.get("/v1/mcp/queries", params={"range": "yesterday"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_mcp_config_reports_real_values(client: TestClient) -> None:
    response = client.get("/v1/mcp/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["revision"] == "rev-1"
    assert payload["disabled_tools"] == ["ingestion_run"]
    assert payload["enabled_tool_count"] == 4
    assert payload["total_tool_count"] == 5
    assert payload["restart_required"] is False
    assert payload["policy"]["max_results"] == 20


def test_mcp_config_is_a_capability_error_before_any_mcp_server_has_published(
    client: TestClient,
    service: MockAppService,
) -> None:
    service.mcp_config_value = None

    response = client.get("/v1/mcp/config")

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "harbor_capability_error"
