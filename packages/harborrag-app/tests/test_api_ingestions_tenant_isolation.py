"""Cross-tenant isolation for the ingestion task routes.

Split out of test_api_ingestions.py to keep that file under the repo's
file-length gate. Covers both /v1/ingestions (the current contract) and the
deprecated /api/v1/ingestions Temporal-run adapters, which used to skip
tenant authorization entirely (``del principal``) -- see legacy_ingestions.py.
"""

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


def _tenant_token(*, tenants: list[str]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "tenant-user",
            "role": "owner",
            "tenants": tenants,
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        _AUTH_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/ingestions/ing_1"),
        ("GET", "/v1/ingestions/ing_1/documents"),
        ("POST", "/v1/ingestions/ing_1/cancel"),
        ("POST", "/v1/ingestions/ing_1/retry-failures"),
    ],
)
def test_cross_tenant_task_ids_are_hidden_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
    method: str,
    path: str,
) -> None:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))
    headers = {"Authorization": f"Bearer {_tenant_token(tenants=['ACME'])}"}

    with TestClient(app) as tenant_client:
        response = tenant_client.request(method, path, headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/ingestions/ing_1"),
        ("GET", "/api/v1/ingestions/ing_1/result"),
        ("POST", "/api/v1/ingestions/ing_1/actions"),
    ],
)
def test_legacy_cross_tenant_task_ids_are_hidden_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
    method: str,
    path: str,
) -> None:
    """Regression: the deprecated Temporal-run adapters used to skip tenant
    authorization entirely (``del principal``); a reader/editor token for one
    tenant must not see or act on another tenant's run, exactly like the
    successor /v1/ingestions routes already enforce."""
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))
    headers = {"Authorization": f"Bearer {_tenant_token(tenants=['ACME'])}"}

    with TestClient(app) as tenant_client:
        response = tenant_client.request(method, path, json={"action": "pause"}, headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"
