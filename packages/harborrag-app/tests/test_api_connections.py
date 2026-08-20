"""Contract tests for GET /v1/connections, the public connection catalog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings

_AUTH_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _token(role: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "catalog-user",
            "role": role,
            "tenants": ["ACME"],
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        _AUTH_SECRET,
        algorithm="HS256",
    )


def test_listing_exposes_only_connection_identity(client: TestClient) -> None:
    """A dropdown needs the submission key and its provider, nothing more."""
    response = client.get("/v1/connections")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"connection_id": "confluence-main", "source_type": "confluence"},
            {"connection_id": "harborrag-workspace", "source_type": "local"},
        ]
    }


def test_listed_connection_ids_are_accepted_by_the_submit_route(
    client: TestClient,
    service: MockAppService,
) -> None:
    """Regression guard: the catalog must name what POST /v1/ingestions takes."""
    listed = client.get("/v1/connections").json()["items"]

    for connection in listed:
        response = client.post(
            "/v1/ingestions",
            json={"connection_id": connection["connection_id"]},
        )
        assert response.status_code == 202
        assert service.submissions[-1].connection_id == connection["connection_id"]


def test_catalog_requires_a_token_under_hmac(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
) -> None:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))

    with TestClient(app) as catalog_client:
        response = catalog_client.get("/v1/connections")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "harbor_auth_error"


def test_reader_role_clears_the_catalog_minimum(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
) -> None:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))

    with TestClient(app) as catalog_client:
        response = catalog_client.get(
            "/v1/connections",
            headers={"Authorization": f"Bearer {_token('reader')}"},
        )

    assert response.status_code == 200
