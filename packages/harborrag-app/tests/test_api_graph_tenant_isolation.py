"""Cross-tenant isolation for the graph conflict routes.

Split out of test_api_graph.py, mirroring test_api_ingestions_tenant_isolation.py:
the rest of that file runs under auth_mode=none, where every caller is a
wildcard principal, so nothing there proves the route itself enforces tenant
scope. The repository-level fakes already cover the scoping logic; these
tests prove the HTTP layer can't be talked around -- resolve is a write with
an audit trail, so it gets the same scrutiny as the list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from app_test_graph_records import graph_conflict
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


def _seed_other_tenant_conflict(service: MockAppService) -> None:
    """Add a second, OTHER-tenant conflict alongside the fixture's DEFAULT-tenant one."""
    other = graph_conflict("gc_other", tenant_id="OTHER")
    service.graph_conflicts[other.id] = other


def test_scoped_principal_cannot_list_another_tenants_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
) -> None:
    _seed_other_tenant_conflict(service)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))
    headers = {"Authorization": f"Bearer {_tenant_token(tenants=['ACME'])}"}

    with TestClient(app) as tenant_client:
        response = tenant_client.get("/v1/graph/conflicts", headers=headers)

    assert response.status_code == 200
    ids = {conflict["id"] for conflict in response.json()["conflicts"]}
    assert "gc_other" not in ids
    assert ids == set()  # the seeded default conflict belongs to tenant DEFAULT, not ACME


def test_resolving_another_tenants_conflict_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
) -> None:
    _seed_other_tenant_conflict(service)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET))
    headers = {"Authorization": f"Bearer {_tenant_token(tenants=['ACME'])}"}

    with TestClient(app) as tenant_client:
        response = tenant_client.post(
            "/v1/graph/conflicts/gc_other/resolve",
            json={"action": "merge"},
            headers=headers,
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"
    assert service.graph_conflict_resolve_calls == []
    assert service.graph_conflicts["gc_other"].status == "open"
