"""Credential, tenant and role checks on every route (Story 2 AC6–AC10, AC19)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_connections import connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.access import api_routes, minimum_role
from harborrag_app.api.auth.principal import ROLE_ORDER
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.projects import ProjectService
from harborrag_core.domain.project import Project
from harborrag_core.ports.api_keys import AuthStoreUnavailable
from harborrag_engine.security.api_key_service import VerifiedKey

_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
_KEY = "hrk_dev_v1_0123456789abcdef01234567." + "k" * 43
_PATH_VALUES = {
    "project_id": "cpm",
    "connection_id": "cpm-tickets",
    "session_id": "session-1",
    "ingestion_id": "ing_1",
}


def _token(
    role: str = "admin",
    tenants: tuple[str, ...] = ("acme",),
    *,
    secret: str = _SECRET,
    expired: bool = False,
    name: str | None = None,
) -> str:
    now = datetime.now(UTC) - (timedelta(hours=2) if expired else timedelta())
    claims: dict[str, object] = {
        "sub": "u_huy",
        "role": role,
        "tenants": list(tenants),
        "iat": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "iss": "harborrag",
        "aud": "harborrag-api",
    }
    if name is not None:
        claims["name"] = name
    return jwt.encode(claims, secret, algorithm="HS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _AccessApp(MockAppService):
    """Mock app service with real project and connection services over fakes."""

    def __init__(self) -> None:
        super().__init__()
        self.world = connection_world(
            [
                Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme"),
                Project(id="gx1", name="Globex", collection="gx1", tenant_id="globex"),
            ]
        )
        self.connection_service = self.world.service
        self.project_service = ProjectService(
            control_plane=lambda: self.world.control_plane,
            runs=self.world.service._runs,
            connections=self.world.service,
        )


class _Keys:
    """API key verifier double: one known key, or a store that is down."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down

    async def verify_key(self, raw_key: str) -> VerifiedKey | None:
        if self.down:
            raise AuthStoreUnavailable("store unreachable")
        if raw_key != _KEY:
            return None
        return VerifiedKey(
            key_id="0123456789abcdef01234567",
            tenant_id="acme",
            owner="svc-auta",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )


@pytest.fixture
def service() -> _AccessApp:
    return _AccessApp()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: _AccessApp) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as test_client:
        app.state.api_key_verifier = _Keys()
        yield test_client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        _bearer(_token(secret="another-secret-that-is-at-least-32-bytes")),
        _bearer(_token(expired=True)),
        _bearer("hrk_dev_v1_ffffffffffffffffffffffff." + "x" * 43),
    ],
    ids=["missing", "bad-signature", "expired", "unknown-key"],
)
def test_a_missing_or_bad_credential_is_one_plain_401(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.get("/v1/admin/connections", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    error = response.json()["error"]
    assert error["code"] == "unauthenticated"
    assert error["message"] == "missing or invalid credentials"


def _routes(client: TestClient) -> list[tuple[str, str, str]]:
    routes = []
    for route in api_routes(client.app.routes):  # type: ignore[attr-defined]
        if route.path.startswith("/v1/"):
            role = minimum_role(route)
            assert role is not None, f"{route.path} has no role check"
            path = re.sub(r"\{(\w+)\}", lambda match: _PATH_VALUES[match.group(1)], route.path)
            routes.extend((method, path, role) for method in sorted(route.methods))
    assert len(routes) >= 20, "the route walk found too few routes to mean anything"
    return routes


@pytest.mark.parametrize("role", ["reader", "editor", "admin"])
def test_a_role_below_the_routes_minimum_is_forbidden(
    client: TestClient, service: _AccessApp, role: str
) -> None:
    def state() -> tuple[object, ...]:
        plane = service.world.control_plane
        return (
            dict(plane.projects.projects),  # type: ignore[attr-defined]
            dict(plane.connections.connections),  # type: ignore[attr-defined]
            dict(service.world.secrets.values),
        )

    before = state()
    headers = _bearer(_token(role))

    for method, path, minimum in _routes(client):
        response = client.request(
            method,
            path,
            headers=headers,
            params={"tenant": "acme"},
            json={"tenant": "acme", "prompt": "hello", "connection_id": "cpm-tickets"},
        )
        if ROLE_ORDER[role] < ROLE_ORDER[minimum]:  # type: ignore[index]
            assert response.status_code == 403, (method, path)
            assert response.json()["error"]["code"] == "forbidden", (method, path)
        else:
            assert response.status_code not in {401, 403}, (method, path, response.text)
        if ROLE_ORDER[role] < ROLE_ORDER["editor"]:  # type: ignore[index]
            assert state() == before, (method, path)


def test_every_v1_route_declares_its_minimum_role(client: TestClient) -> None:
    roles = {(method, path): role for method, path, role in _routes(client)}

    assert roles[("GET", "/v1/me")] == "reader"
    assert roles[("GET", "/v1/admin/source-types")] == "editor"
    assert roles[("PUT", "/v1/admin/projects/cpm/connections/cpm-tickets")] == "editor"
    assert roles[("DELETE", "/v1/admin/projects/cpm")] == "admin"
    assert roles[("POST", "/v1/chat/completions")] == "reader"
    assert roles[("GET", "/v1/projects/cpm/connections")] == "reader"


def test_another_tenants_project_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/admin/projects/gx1", headers=_bearer(_token()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/admin/projects"),
        ("GET", "/v1/admin/source-types"),
        ("GET", "/v1/chat/sessions"),
        ("GET", "/v1/admin/ingestions"),
        ("GET", "/v1/me"),
    ],
)
def test_a_tenant_the_credential_does_not_hold_is_refused(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path, headers={**_bearer(_token()), "X-Tenant-Id": "globex"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"


def test_several_tenants_need_the_header(client: TestClient) -> None:
    token = _token(tenants=("acme", "globex"))

    missing = client.get("/v1/admin/projects", headers=_bearer(token))
    chosen = client.get("/v1/admin/projects", headers={**_bearer(token), "X-Tenant-Id": "globex"})

    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "tenant_required"
    assert chosen.status_code == 200
    assert [item["tenant_id"] for item in chosen.json()["items"]] == ["globex"]


def test_the_tenant_check_runs_before_the_role_check(client: TestClient) -> None:
    response = client.get(
        "/v1/admin/projects", headers=_bearer(_token("reader", ("acme", "globex")))
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "tenant_required"


def test_me_describes_a_user_token(client: TestClient) -> None:
    response = client.get("/v1/me", headers=_bearer(_token(name="Huy Nguyen")))

    assert response.status_code == 200
    assert response.json() == {
        "principal": {"type": "user", "id": "u_huy", "name": "Huy Nguyen"},
        "tenant_id": "acme",
        "tenants": ["acme"],
        "role": "admin",
    }


def test_me_lists_every_tenant_without_a_header(client: TestClient) -> None:
    response = client.get("/v1/me", headers=_bearer(_token(tenants=("globex", "acme"))))

    assert response.status_code == 200
    assert response.json()["tenants"] == ["acme", "globex"]
    assert response.json()["tenant_id"] is None


def test_an_api_key_is_a_reader_in_its_tenant(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG"):
        me = client.get("/v1/me", headers=_bearer(_KEY))
        editor_route = client.get("/v1/admin/connections", headers=_bearer(_KEY))

    assert me.status_code == 200
    assert me.json() == {
        "principal": {"type": "api_key", "id": "0123456789abcdef01234567", "name": "svc-auta"},
        "tenant_id": "acme",
        "tenants": ["acme"],
        "role": "reader",
    }
    assert editor_route.status_code == 403
    assert _KEY not in me.text + editor_route.text + caplog.text


def test_an_unreachable_key_store_is_unavailable_not_unauthenticated(
    client: TestClient,
) -> None:
    client.app.state.api_key_verifier = _Keys(down=True)  # type: ignore[attr-defined]

    response = client.get("/v1/me", headers=_bearer(_KEY))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"


def test_an_unknown_key_is_checked_against_the_control_database() -> None:
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))

    with TestClient(app) as test_client:
        response = test_client.get("/v1/me", headers=_bearer(_KEY))

    assert response.status_code == 401
    assert app.state.api_key_verifier is not None
