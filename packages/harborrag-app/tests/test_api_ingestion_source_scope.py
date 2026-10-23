"""An explicit source scope chooses an ACL pile, so only admins may name one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


def _token(role: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "u1",
            "role": role,
            "tenants": ["DEFAULT"],
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET)
    with TestClient(create_fastapi_app(settings), raise_server_exceptions=False) as c:
        yield c


def _post(client: TestClient, role: str, body: dict) -> object:
    return client.post(
        "/v1/ingestions", json=body, headers={"Authorization": f"Bearer {_token(role)}"}
    )


def test_editor_naming_a_source_scope_is_forbidden(client, service) -> None:
    response = _post(
        client,
        "editor",
        {"connection_id": "harborrag-workspace", "source_scope_id": "eng-handbook"},
    )

    assert response.status_code == 403
    assert service.submissions == []


def test_admin_may_name_the_source_scope(client, service) -> None:
    response = _post(
        client,
        "admin",
        {"connection_id": "harborrag-workspace", "source_scope_id": "eng-handbook"},
    )

    assert response.status_code == 202
    assert service.submissions[-1].source_scope_id == "eng-handbook"


def test_editor_may_still_ingest_without_naming_a_scope(client, service) -> None:
    response = _post(client, "editor", {"connection_id": "harborrag-workspace"})

    assert response.status_code == 202
    assert service.submissions[-1].source_scope_id is None
