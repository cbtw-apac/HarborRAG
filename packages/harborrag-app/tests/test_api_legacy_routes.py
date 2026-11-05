"""Legacy and non-explorer routes are gone, with no deprecated alias left behind."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings

REMOVED = [
    ("GET", "/api/v1/projects"),
    ("GET", "/api/v1/projects/cpm"),
    ("GET", "/api/v1/sources"),
    ("POST", "/api/v1/sources"),
    ("GET", "/v1/connections"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/readyz"),
    ("GET", "/api/v1/metrics"),
    ("GET", "/api/v1/diagnostics"),
    ("GET", "/api/v1/activity"),
    ("GET", "/api/v1/settings"),
    ("POST", "/api/v1/ingestions"),
    ("POST", "/v1/agent/completions"),
    ("GET", "/v1/agent/sessions"),
    ("POST", "/v1/agent/sessions"),
    ("POST", "/v1/agent/runs/run-1/resume"),
    ("POST", "/v1/retrieval/vector"),
    ("POST", "/v1/graph/traverse"),
    ("GET", "/v1/graph/overview"),
    ("GET", "/v1/memory/memories"),
    ("DELETE", "/v1/memory/sessions/session-1"),
    ("GET", "/v1/schedules"),
    ("GET", "/v1/admin/projections/DEFAULT"),
    ("PATCH", "/v1/chat/sessions/session-1"),
    ("DELETE", "/v1/chat/sessions/session-1"),
    ("POST", "/v1/admin/projects/cpm/connections/cpm-tickets/sync"),
    ("POST", "/v1/ingestions"),
    ("GET", "/v1/ingestions"),
    ("GET", "/v1/ingestions/ing_1"),
    ("GET", "/v1/ingestions/ing_1/stream"),
    ("POST", "/v1/ingestions/ing_1/cancel"),
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    service = MockAppService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        yield client


@pytest.mark.parametrize(("method", "path"), REMOVED)
def test_a_removed_route_is_a_plain_not_found(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, json={})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers


@pytest.mark.parametrize(
    "path",
    ["/healthz", "/v1/admin/ingestions", "/v1/chat/sessions"],
)
def test_a_live_route_announces_no_deprecation(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers
    assert "link" not in response.headers


def test_openapi_lists_no_removed_path(client: TestClient) -> None:
    paths = set(client.app.openapi()["paths"])  # type: ignore[attr-defined]

    assert not {path for _, path in REMOVED} & paths
    assert not [path for path in paths if path.startswith(("/api/", "/v1/ingestions"))]


def test_ingestion_routes_share_one_prefix_and_one_tag(client: TestClient) -> None:
    openapi = client.app.openapi()  # type: ignore[attr-defined]
    ingestion = {
        path
        for path, item in openapi["paths"].items()
        for operation in item.values()
        if "Ingestion" in operation.get("tags", [])
    }

    assert ingestion and all(path.startswith("/v1/admin/ingestions") for path in ingestion)
    assert not [tag for tag in openapi.get("tags", []) if tag.get("name") == "Ingestions"]
    assert not [
        path
        for path, item in openapi["paths"].items()
        for operation in item.values()
        if "Ingestions" in operation.get("tags", [])
    ]
