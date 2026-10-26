"""Deprecated /api/v1/ingestions error bodies keep the run they are about."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control import AppResponse

_AUTH_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


def _token() -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "editor",
            "role": "owner",
            "tenants": ["ACME"],
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        _AUTH_SECRET,
        algorithm="HS256",
    )


def _post(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> dict[str, object]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/ingestions",
            json={"tenant_id": "ACME", "connector_name": "workspace", "run_id": "run-down"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 502
    assert response.headers["Deprecation"] == "true"
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_connect_failure_names_the_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unreachable(MockAppService):
        async def start_ingestion(self, **kwargs):  # type: ignore[override]
            return AppResponse(
                False,
                {
                    "run": {"run_id": kwargs["run_id"], "status": "FAILED"},
                    "error_type": "RuntimeConnectionError",
                },
                "Temporal is unreachable at localhost:7233",
            )

    details = _post(monkeypatch, Unreachable())["error"]["details"]  # type: ignore[index]

    assert details == {
        "operation": "start",
        "error_type": "RuntimeConnectionError",
        "run": {"run_id": "run-down", "status": "FAILED"},
    }


def test_failure_without_a_run_keeps_the_old_details(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(MockAppService):
        async def start_ingestion(self, **kwargs):  # type: ignore[override]
            return AppResponse(False, {"error_type": "RuntimeError"}, "boom")

    details = _post(monkeypatch, Broken())["error"]["details"]  # type: ignore[index]

    assert details == {"operation": "start", "error_type": "RuntimeError"}
