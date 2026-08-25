"""Contract tests for GET /v1/ingestions, the public task list.

Kept out of test_api_ingestions.py to stay under the repo's file-length gate.
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


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _tenant_token(*, tenants: list[str], role: str = "reader") -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "tenant-user",
            "role": role,
            "tenants": tenants,
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        _AUTH_SECRET,
        algorithm="HS256",
    )


def test_listing_returns_the_get_by_id_task_shape(
    client: TestClient,
    service: MockAppService,
) -> None:
    """The table and the detail view render from the same task fields."""
    listed = client.get("/v1/ingestions")
    detail = client.get("/v1/ingestions/00000000-0000-4000-8000-000000000001")

    assert listed.status_code == 200
    assert set(listed.json()) == {"items", "next_cursor"}
    assert listed.json()["items"] == [detail.json()]
    assert listed.json()["next_cursor"] is not None


def test_default_query_reads_the_callers_full_scope(
    client: TestClient,
    service: MockAppService,
) -> None:
    """auth_mode=none is a wildcard principal, so no tenant filter is applied."""
    assert client.get("/v1/ingestions").status_code == 200
    assert service.task_list_calls[-1] == {
        "tenants": None,
        "statuses": None,
        "cursor": None,
        "limit": 50,
    }


def test_filters_and_cursor_reach_the_application_service(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.get(
        "/v1/ingestions",
        params={"tenant": "ACME", "status": "SUCCESS", "cursor": "abc", "limit": 5},
    )

    assert response.status_code == 200
    assert service.task_list_calls[-1] == {
        "tenants": frozenset({"ACME"}),
        "statuses": ["SUCCESS"],
        "cursor": "abc",
        "limit": 5,
    }


def test_repeated_status_reaches_the_application_service_as_one_filter(
    client: TestClient,
    service: MockAppService,
) -> None:
    """?status=PENDING&status=RUNNING ORs both, instead of costing two calls."""
    response = client.get(
        "/v1/ingestions",
        params=[("status", "PENDING"), ("status", "RUNNING")],
    )

    assert response.status_code == 200
    assert service.task_list_calls[-1]["statuses"] == ["PENDING", "RUNNING"]


def test_malformed_cursor_is_rejected_at_the_route(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.get("/v1/ingestions", params={"cursor": "not-base64"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INGESTION_CURSOR_INVALID"
    assert service.task_list_calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"status": "NOT_A_STATUS"},
        {"tenant": ""},
        {"unknown": "1"},
    ],
)
def test_invalid_query_parameters_are_rejected(
    client: TestClient,
    service: MockAppService,
    params: dict[str, object],
) -> None:
    response = client.get("/v1/ingestions", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"
    assert service.task_list_calls == []


def test_scoped_principal_cannot_list_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
) -> None:
    """An explicit out-of-scope tenant is a rejected filter, not a hidden task."""
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))
    headers = {"Authorization": f"Bearer {_tenant_token(tenants=['ACME'])}"}

    with TestClient(app) as tenant_client:
        response = tenant_client.get("/v1/ingestions", params={"tenant": "OTHER"}, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "harbor_auth_error"
    assert service.task_list_calls == []


def test_scoped_principal_without_a_tenant_filter_reads_only_its_own(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
) -> None:
    """Omitting ``tenant`` must narrow to the token's tenants, never widen."""
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))
    headers = {"Authorization": f"Bearer {_tenant_token(tenants=['ACME', 'BETA'])}"}

    with TestClient(app) as tenant_client:
        response = tenant_client.get("/v1/ingestions", headers=headers)

    assert response.status_code == 200
    assert service.task_list_calls[-1]["tenants"] == frozenset({"ACME", "BETA"})


def test_listing_requires_a_token_under_hmac(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
) -> None:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))

    with TestClient(app) as tenant_client:
        response = tenant_client.get("/v1/ingestions")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "harbor_auth_error"
