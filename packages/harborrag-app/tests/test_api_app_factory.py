"""create_fastapi_app boots with routes, metrics, middleware, and envelopes."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.schemas import AppResponse


@pytest.mark.blackbox
def test_health_and_openapi_served() -> None:
    """/health answers ok and the OpenAPI schema is published under /api/v1."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        readiness = client.get("/api/v1/readyz")
        assert readiness.status_code == 200
        assert readiness.json()["status"] == "ready"
        schema = client.get("/api/v1/openapi.json")
        assert schema.status_code == 200
        assert schema.json()["info"]["title"] == "HarborRAG Control Plane API"


@pytest.mark.blackbox
def test_convenience_routes_redirect_to_canonical_docs() -> None:
    """Browser-friendly root paths redirect without entering the API schema."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        root = client.get("/", follow_redirects=False)
        docs = client.get("/docs", follow_redirects=False)

        assert root.status_code == 307
        assert root.headers["location"] == "/api/v1/docs"
        assert docs.status_code == 307
        assert docs.headers["location"] == "/api/v1/docs"
        assert "/" not in client.get("/api/v1/openapi.json").json()["paths"]


@pytest.mark.blackbox
def test_metrics_exposes_api_and_process_observations() -> None:
    """Prometheus receives low-cardinality HTTP and process observations."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        client.get("/api/v1/health")
        response = client.get("/api/v1/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain;")
        assert "harborrag_api_info" in response.text
        assert "process_resident_memory_bytes" in response.text
        request_lines = [
            line
            for line in response.text.splitlines()
            if line.startswith("harborrag_api_http_requests_total{")
        ]
        assert request_lines == [
            'harborrag_api_http_requests_total{method="GET",route="/api/v1/health",'
            'status_code="200"} 1.0'
        ]
        assert 'status_code="200"' in response.text


@pytest.mark.blackbox
def test_legacy_diagnostics_is_deprecated_and_redacted() -> None:
    settings = ApiSettings(auth_secret="diagnostics-must-not-expose-this-secret")
    with TestClient(create_fastapi_app(settings)) as client:
        response = client.get("/api/v1/diagnostics")

    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    assert response.json()["settings"]["auth_secret"] == "<redacted>"
    assert "diagnostics-must-not-expose-this-secret" not in response.text


@pytest.mark.blackbox
def test_readiness_reports_an_unavailable_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MockAppService()
    monkeypatch.setattr(
        service,
        "health",
        lambda: AppResponse(False, error="runtime not ready"),
    )
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))

    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "version": "0.1.0"}


@pytest.mark.blackbox
def test_oversized_request_body_is_rejected_before_validation() -> None:
    settings = ApiSettings(max_request_body_bytes=1_024)
    with TestClient(create_fastapi_app(settings)) as client:
        response = client.post(
            "/v1/retrieval/vector",
            json={"query": "x" * 2_000},
            headers={"X-Request-Id": "oversized-request"},
        )

    assert response.status_code == 413
    assert response.headers["x-request-id"] == "oversized-request"
    assert response.json()["error"] == {
        "code": "request_too_large",
        "message": "Request body exceeds the configured limit",
        "details": {},
        "trace_id": "oversized-request",
    }


@pytest.mark.blackbox
def test_trace_id_minted_and_echoed() -> None:
    """Responses carry X-Request-Id; a client-provided id is echoed verbatim."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        minted = client.get("/api/v1/health")
        assert minted.headers.get("x-request-id")
        echoed = client.get("/api/v1/health", headers={"X-Request-Id": "trace-123"})
        assert echoed.headers["x-request-id"] == "trace-123"


@pytest.mark.blackbox
@pytest.mark.parametrize("invalid", ["", "x" * 65, "spaces are invalid", "forged\ntrace"])
def test_invalid_trace_id_is_replaced(invalid: str) -> None:
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/health", headers={"X-Request-Id": invalid})
        trace_id = response.headers["x-request-id"]
        assert trace_id != invalid
        assert len(trace_id) == 32


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
    """docs_enabled=False closes docs while root remains a useful health link."""
    with TestClient(
        create_fastapi_app(ApiSettings(docs_enabled=False)),
        raise_server_exceptions=False,
    ) as client:
        assert client.get("/api/v1/docs").status_code == 404
        assert client.get("/docs").status_code == 404
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/api/v1/health"


@pytest.mark.blackbox
def test_docs_default_to_disabled_in_prod() -> None:
    """env=prod must not silently expose Swagger docs (or the raw OpenAPI
    schema route) via the docs_enabled=True default."""
    settings = ApiSettings(
        env="prod",
        auth_mode="hmac",
        auth_secret="production-test-secret-at-least-32-bytes",
        api_capacity_redis_url="rediss://localhost:6379/1",
    )
    assert settings.docs_enabled is False
    with TestClient(
        create_fastapi_app(settings),
        raise_server_exceptions=False,
    ) as client:
        assert client.get("/api/v1/docs").status_code == 404
        assert client.get("/api/v1/openapi.json").status_code == 404


@pytest.mark.blackbox
def test_prod_requires_distributed_api_capacity_backend() -> None:
    with pytest.raises(ValueError, match="API_CAPACITY_REDIS_URL is required"):
        ApiSettings(
            env="prod",
            auth_mode="hmac",
            auth_secret="production-test-secret-at-least-32-bytes",
        )


@pytest.mark.blackbox
def test_remote_plaintext_capacity_redis_requires_development_acknowledgement() -> None:
    with pytest.raises(ValueError, match="encrypted transport"):
        ApiSettings(api_capacity_redis_url="redis://redis.internal:6379/1")

    settings = ApiSettings(
        api_capacity_redis_url="redis://redis.internal:6379/1",
        api_capacity_allow_insecure_remote=True,
    )
    assert settings.api_capacity_redis_url is not None


@pytest.mark.blackbox
def test_prod_rejects_plaintext_capacity_redis_even_with_acknowledgement() -> None:
    with pytest.raises(ValueError, match="encrypted transport"):
        ApiSettings(
            env="prod",
            auth_mode="hmac",
            auth_secret="production-test-secret-at-least-32-bytes",
            api_capacity_redis_url="redis://redis.internal:6379/1",
            api_capacity_allow_insecure_remote=True,
        )


@pytest.mark.blackbox
def test_docs_explicit_true_is_respected_even_in_prod() -> None:
    """An operator who explicitly opts in to docs in prod must still get them."""
    settings = ApiSettings(
        env="prod",
        auth_mode="hmac",
        auth_secret="production-test-secret-at-least-32-bytes",
        api_capacity_redis_url="rediss://localhost:6379/1",
        docs_enabled=True,
    )
    assert settings.docs_enabled is True
    with TestClient(create_fastapi_app(settings)) as client:
        assert client.get("/api/v1/docs").status_code == 200


@pytest.mark.blackbox
def test_wildcard_cors_origin_is_rejected() -> None:
    """'*' in cors_origins must fail at factory time — credentialed CORS
    with a wildcard origin is never allowed."""
    from harborrag_core.contracts.errors import HarborConfigurationError

    with pytest.raises(HarborConfigurationError):
        create_fastapi_app(ApiSettings(cors_origins=["*"]))


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
