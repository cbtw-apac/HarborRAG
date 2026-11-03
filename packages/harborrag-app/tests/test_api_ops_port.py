"""``/metrics`` answers only on the ops port, only to the scrape token (Story 2 AC16)."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.serve import bind_sockets
from harborrag_app.api.settings import ApiSettings

_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
_SCRAPE = "scrape-token-0123456789abcdef"
_OPS = "http://testserver:9465"


def _admin_token() -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "u_admin",
            "role": "admin",
            "tenants": ["acme"],
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        _SECRET,
        algorithm="HS256",
    )


def _settings(**overrides: object) -> ApiSettings:
    values: dict[str, object] = {
        "auth_mode": "hmac",
        "auth_secret": _SECRET,
        "ops_port": 9465,
        "metrics_scrape_token": SecretStr(_SCRAPE),
        **overrides,
    }
    return ApiSettings(**values)  # type: ignore[arg-type]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    service = MockAppService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(_settings())) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "authorization", [None, f"Bearer {_admin_token()}", "Bearer wrong"], ids=["none", "jwt", "bad"]
)
def test_the_ops_port_wants_the_scrape_token(client: TestClient, authorization: str | None) -> None:
    headers = {"Authorization": authorization} if authorization else {}

    response = client.get(f"{_OPS}/metrics", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]


def test_the_scrape_token_reads_the_metrics(client: TestClient) -> None:
    client.get("/v1/ingestions/ing_1")

    response = client.get(f"{_OPS}/metrics", headers={"Authorization": f"Bearer {_SCRAPE}"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert "# TYPE harborrag_api_http_requests_total counter" in response.text
    assert 'route="/v1/ingestions/{task_id}"' in response.text
    assert "harborrag_api_info" in response.text
    assert "process_resident_memory_bytes" in response.text


def test_the_public_port_has_no_metrics(client: TestClient) -> None:
    response = client.get("/metrics", headers={"Authorization": f"Bearer {_SCRAPE}"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("path", ["/healthz", "/v1/me", "/docs"])
def test_the_ops_port_serves_nothing_else(client: TestClient, path: str) -> None:
    response = client.get(f"{_OPS}{path}", headers={"Authorization": f"Bearer {_SCRAPE}"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_without_an_ops_port_no_port_serves_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MockAppService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(_settings(ops_port=None))) as test_client:
        response = test_client.get(
            f"{_OPS}/metrics", headers={"Authorization": f"Bearer {_SCRAPE}"}
        )

    assert response.status_code == 404


def test_without_a_scrape_token_the_ops_port_refuses_everyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MockAppService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(_settings(metrics_scrape_token=None))) as test_client:
        response = test_client.get(f"{_OPS}/metrics", headers={"Authorization": "Bearer "})

    assert response.status_code == 401


def test_the_ops_port_must_differ_from_the_public_port() -> None:
    with pytest.raises(ValidationError, match="HARBORRAG_OPS_PORT"):
        ApiSettings(port=8000, ops_port=8000)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_launcher_listens_on_both_ports() -> None:
    public, ops = _free_port(), _free_port()

    sockets = bind_sockets(ApiSettings(host="127.0.0.1", port=public, ops_port=ops))
    try:
        assert sorted(sock.getsockname()[1] for sock in sockets) == sorted([public, ops])
    finally:
        for sock in sockets:
            sock.close()


def test_the_launcher_listens_on_the_public_port_alone_without_an_ops_port() -> None:
    public = _free_port()

    sockets = bind_sockets(ApiSettings(host="127.0.0.1", port=public, ops_port=None))
    try:
        assert [sock.getsockname()[1] for sock in sockets] == [public]
    finally:
        for sock in sockets:
            sock.close()
