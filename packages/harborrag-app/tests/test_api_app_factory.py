"""create_fastapi_app boots with routers, middleware, and envelopes (ST2/ST9)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


@pytest.mark.blackbox
def test_health_and_openapi_served() -> None:
    """/health answers ok and the OpenAPI schema is published under /api/v1."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        schema = client.get("/api/v1/openapi.json")
        assert schema.status_code == 200
        assert schema.json()["info"]["title"] == "HarborRAG Control Plane API"


@pytest.mark.blackbox
def test_readyz_ready_with_mock_composition() -> None:
    """/readyz reports ready when the (mock) app service is healthy."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


@pytest.mark.blackbox
def test_trace_id_minted_and_echoed() -> None:
    """Responses carry X-Request-Id; a client-provided id is echoed verbatim."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        minted = client.get("/api/v1/health")
        assert minted.headers.get("x-request-id")
        echoed = client.get("/api/v1/health", headers={"X-Request-Id": "trace-123"})
        assert echoed.headers["x-request-id"] == "trace-123"


@pytest.mark.blackbox
def test_unknown_route_returns_enveloped_404_with_trace_id() -> None:
    """Framework 404s use the error envelope and carry the request trace id."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/nope", headers={"X-Request-Id": "trace-404"})
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "not_found"
        assert body["error"]["trace_id"] == "trace-404"


@pytest.mark.blackbox
def test_docs_disabled_by_settings() -> None:
    """docs_enabled=False removes the Swagger UI route."""
    with TestClient(
        create_fastapi_app(ApiSettings(docs_enabled=False)),
        raise_server_exceptions=False,
    ) as client:
        assert client.get("/api/v1/docs").status_code == 404


@pytest.mark.blackbox
def test_cors_honors_configured_origins() -> None:
    """Configured origins get CORS headers; without config no header appears."""
    origin = "http://webui.local"
    with TestClient(create_fastapi_app(ApiSettings(cors_origins=[origin]))) as client:
        response = client.get("/api/v1/health", headers={"Origin": origin})
        assert response.headers.get("access-control-allow-origin") == origin
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/health", headers={"Origin": origin})
        assert "access-control-allow-origin" not in response.headers
