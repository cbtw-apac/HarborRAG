"""RBAC enforcement on the ML1 read routes (reader role minimum).

Mirrors test_api_auth.py's diagnostics coverage: these routes must not be
reachable without at least a reader-role token once auth_mode=hmac.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
READ_ROUTES = [
    "/api/v1/projects",
    "/api/v1/sources",
    "/api/v1/activity",
    "/api/v1/settings",
    "/api/v1/metrics/ingestion",
]


def _token(role: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": "u1",
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "iss": "harborrag",
        "aud": "harborrag-api",
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _hmac_client() -> TestClient:
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET)
    return TestClient(create_fastapi_app(settings), raise_server_exceptions=False)


@pytest.mark.blackbox
@pytest.mark.parametrize("path", READ_ROUTES)
def test_none_mode_reaches_every_read_route(path: str) -> None:
    """auth_mode=none (dev default) grants the implicit owner, no token needed."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        assert client.get(path).status_code == 200


@pytest.mark.blackbox
@pytest.mark.parametrize("path", READ_ROUTES)
def test_hmac_mode_rejects_missing_token(path: str) -> None:
    """Every ML1 read route requires at least a reader-role bearer token."""
    with _hmac_client() as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "harbor_auth_error"


@pytest.mark.blackbox
@pytest.mark.parametrize("path", READ_ROUTES)
def test_hmac_mode_accepts_reader_role(path: str) -> None:
    """A reader-role token clears the minimum on every ML1 read route."""
    with _hmac_client() as client:
        response = client.get(path, headers={"Authorization": f"Bearer {_token('reader')}"})
        assert response.status_code == 200
