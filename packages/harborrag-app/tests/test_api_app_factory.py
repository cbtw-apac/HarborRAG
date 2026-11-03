"""create_fastapi_app boots with routes, metrics, middleware, and envelopes."""

from __future__ import annotations

from typing import Literal

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


@pytest.mark.blackbox
def test_health_and_openapi_served() -> None:
    """/healthz answers ok and the OpenAPI schema is published at the root."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert schema.json()["info"]["title"] == "HarborRAG Control Plane API"


@pytest.mark.blackbox
def test_root_redirects_to_the_docs() -> None:
    """The root redirects to Swagger without entering the API schema."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        root = client.get("/", follow_redirects=False)

        assert root.status_code == 307
        assert root.headers["location"] == "/docs"
        assert client.get("/docs").status_code == 200
        assert "/" not in client.get("/openapi.json").json()["paths"]


@pytest.mark.blackbox
def test_oversized_request_body_is_rejected_before_validation() -> None:
    settings = ApiSettings(max_request_body_bytes=1_024)
    with TestClient(create_fastapi_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
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
        minted = client.get("/healthz")
        assert minted.headers.get("x-request-id")
        echoed = client.get("/healthz", headers={"X-Request-Id": "trace-123"})
        assert echoed.headers["x-request-id"] == "trace-123"


@pytest.mark.blackbox
@pytest.mark.parametrize("invalid", ["", "x" * 65, "spaces are invalid", "forged\ntrace"])
def test_invalid_trace_id_is_replaced(invalid: str) -> None:
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/healthz", headers={"X-Request-Id": invalid})
        trace_id = response.headers["x-request-id"]
        assert trace_id != invalid
        assert len(trace_id) == 32


@pytest.mark.blackbox
def test_unknown_route_returns_enveloped_404_with_trace_id() -> None:
    """Framework 404s use the error envelope and carry the request trace id."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/v1/nope", headers={"X-Request-Id": "trace-404"})
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
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/healthz"


@pytest.mark.blackbox
@pytest.mark.parametrize("env", ["staging", "prod"])
def test_docs_default_to_disabled_in_prod(env: Literal["staging", "prod"]) -> None:
    """env=prod must not silently expose Swagger docs (or the raw OpenAPI
    schema route) via the docs_enabled=True default."""
    settings = ApiSettings(
        env=env,
        auth_mode="hmac",
        auth_secret="production-test-secret-at-least-32-bytes",
        api_capacity_redis_url="rediss://localhost:6379/1",
    )
    assert settings.docs_enabled is False
    with TestClient(
        create_fastapi_app(settings),
        raise_server_exceptions=False,
    ) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


@pytest.mark.blackbox
@pytest.mark.parametrize("env", ["staging", "prod"])
def test_prod_requires_distributed_api_capacity_backend(env: Literal["staging", "prod"]) -> None:
    with pytest.raises(ValueError, match="API_CAPACITY_REDIS_URL is required"):
        ApiSettings(
            env=env,
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
@pytest.mark.parametrize("env", ["staging", "prod"])
def test_prod_rejects_plaintext_capacity_redis_even_with_acknowledgement(
    env: Literal["staging", "prod"],
) -> None:
    with pytest.raises(ValueError, match="encrypted transport"):
        ApiSettings(
            env=env,
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
        assert client.get("/docs").status_code == 200


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
        response = client.get("/healthz", headers={"Origin": origin})
        assert response.headers.get("access-control-allow-origin") == origin
        exposed = {
            value.strip().lower()
            for value in response.headers["access-control-expose-headers"].split(",")
        }
        assert {"x-request-id", "idempotency-replayed", "retry-after"} <= exposed
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/healthz", headers={"Origin": origin})
        assert "access-control-allow-origin" not in response.headers
