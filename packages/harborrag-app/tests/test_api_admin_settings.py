"""HTTP contract of `/v1/admin/settings[/schema]`."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.settings import SettingsService
from harborrag_core.domain.connection import Connection
from harborrag_core.domain.project import Project
from harborrag_core.domain.setting_override import SettingOverride
from harborrag_core.testing.control_plane_fakes import (
    FakeConnectionRepository,
    FakeProjectRepository,
    FakeSettingOverrideRepository,
    FakeSettingScopeRevisionRepository,
)


def _seed_cascade_fixture(app: _SettingsApp) -> None:
    """chunking.max_tokens: 512 at tenant, 384 at project cpm, 256 at connection cpm/cpm-tickets."""
    overrides = app.cp.setting_overrides.overrides
    overrides[("acme", "tenant", "acme", "chunking.max_tokens")] = SettingOverride(
        tenant_id="acme",
        scope_level="tenant",
        scope_id="acme",
        key="chunking.max_tokens",
        value=512,
    )
    overrides[("acme", "project", "cpm", "chunking.max_tokens")] = SettingOverride(
        tenant_id="acme",
        scope_level="project",
        scope_id="cpm",
        key="chunking.max_tokens",
        value=384,
    )
    overrides[("acme", "connection", "cpm/cpm-tickets", "chunking.max_tokens")] = SettingOverride(
        tenant_id="acme",
        scope_level="connection",
        scope_id="cpm/cpm-tickets",
        key="chunking.max_tokens",
        value=256,
    )
    app.cp.setting_scope_revisions.revisions[("acme", "project", "cpm")] = 41


_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


@dataclass(slots=True)
class _CP:
    """Just the ports SettingsService touches."""

    setting_overrides: FakeSettingOverrideRepository
    setting_scope_revisions: FakeSettingScopeRevisionRepository
    projects: FakeProjectRepository
    connections: FakeConnectionRepository


def _project(project_id: str, tenant_id: str = "acme") -> Project:
    return Project(id=project_id, name=project_id, collection=project_id, tenant_id=tenant_id)


def _connection(project_id: str, connection_id: str, tenant_id: str = "acme") -> Connection:
    return Connection(
        id=f"{tenant_id}-{project_id}-{connection_id}",
        tenant_id=tenant_id,
        project_id=project_id,
        connection_id=connection_id,
        source_key=f"{project_id}/{connection_id}",
        source_type="jira",
        name=connection_id,
    )


class _SettingsApp(MockAppService):
    """Mock app service carrying a real SettingsService over fakes."""

    def __init__(self) -> None:
        super().__init__()
        self.cp = _CP(
            FakeSettingOverrideRepository(),
            FakeSettingScopeRevisionRepository(),
            FakeProjectRepository({"cpm": _project("cpm"), "other": _project("other")}),
            FakeConnectionRepository(
                {
                    "cpm-tickets": _connection("cpm", "cpm-tickets"),
                    "cpm-docs": _connection("cpm", "cpm-docs"),
                }
            ),
        )
        self.settings_service = SettingsService(control_plane=lambda: self.cp)


def _token(role: str, tenants: list[str]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "u_huy",
            "role": role,
            "tenants": tenants,
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        _SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def app_service() -> _SettingsApp:
    return _SettingsApp()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, app_service: _SettingsApp) -> Iterator[TestClient]:
    settings = ApiSettings(auth_mode="hmac", auth_secret=_SECRET)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (app_service, "test"))
    with TestClient(create_fastapi_app(settings)) as test_client:
        yield test_client


def _auth(role: str, tenants: list[str] | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(role, tenants or ['acme'])}"}


def test_schema_lists_every_group_for_an_editor(client: TestClient) -> None:
    response = client.get("/v1/admin/settings/schema", headers=_auth("editor"))
    assert response.status_code == 200
    body = response.json()
    assert [g["name"] for g in body["groups"]] == [
        "capture",
        "parsing",
        "chunking",
        "embedding",
        "ingestion",
    ]


def test_schema_refuses_a_reader(client: TestClient) -> None:
    response = client.get("/v1/admin/settings/schema", headers=_auth("reader"))
    assert response.status_code == 403


def test_admin_reads_tenant_level_and_gets_an_etag(client: TestClient) -> None:
    response = client.get("/v1/admin/settings", headers=_auth("admin"), params={"level": "tenant"})
    assert response.status_code == 200
    assert response.headers["ETag"] == 'W/"rev-0"'
    assert response.json() == {"level": "tenant", "revision": "rev_0", "values": {}}


def test_editor_is_refused_tenant_level_read(client: TestClient) -> None:
    response = client.get("/v1/admin/settings", headers=_auth("editor"), params={"level": "tenant"})
    assert response.status_code == 403


def test_editor_may_read_connection_level(client: TestClient) -> None:
    response = client.get(
        "/v1/admin/settings",
        headers=_auth("editor"),
        params={"level": "connection:cpm/cpm-tickets"},
    )
    assert response.status_code == 200


def test_admin_patch_requires_if_match(client: TestClient) -> None:
    response = client.patch(
        "/v1/admin/settings",
        headers=_auth("admin"),
        params={"level": "tenant"},
        json={"chunking.max_tokens": 384},
    )
    assert response.status_code == 422  # FastAPI's own missing-header validation


def test_editor_cannot_patch(client: TestClient) -> None:
    response = client.patch(
        "/v1/admin/settings",
        headers={**_auth("editor"), "If-Match": 'W/"rev-0"'},
        params={"level": "tenant"},
        json={"chunking.max_tokens": 384},
    )
    assert response.status_code == 403


def test_admin_patch_then_read_effective_round_trips(client: TestClient) -> None:
    patched = client.patch(
        "/v1/admin/settings",
        headers={**_auth("admin"), "If-Match": 'W/"rev-0"'},
        params={"level": "tenant"},
        json={"chunking.max_tokens": 384},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["revision"] == "rev_1"
    assert body["changed"] == [
        {"key": "chunking.max_tokens", "from": 1100, "to": 384, "applies": "next_ingestion"}
    ]
    assert patched.headers["ETag"] == 'W/"rev-1"'

    effective = client.get(
        "/v1/admin/settings",
        headers=_auth("admin"),
        params={"level": "tenant", "effective": "true"},
    )
    assert effective.json()["values"]["chunking.max_tokens"] == {
        "value": 384,
        "origin": "tenant",
    }


def test_stale_if_match_is_refused(client: TestClient) -> None:
    client.patch(
        "/v1/admin/settings",
        headers={**_auth("admin"), "If-Match": 'W/"rev-0"'},
        params={"level": "tenant"},
        json={"chunking.max_tokens": 384},
    )
    stale = client.patch(
        "/v1/admin/settings",
        headers={**_auth("admin"), "If-Match": 'W/"rev-0"'},
        params={"level": "tenant"},
        json={"chunking.max_tokens": 512},
    )
    assert stale.status_code == 412


def test_dry_run_does_not_write(client: TestClient) -> None:
    response = client.patch(
        "/v1/admin/settings",
        headers={**_auth("admin"), "If-Match": 'W/"rev-0"'},
        params={"level": "tenant", "dry_run": "true"},
        json={"chunking.max_tokens": 384},
    )
    assert response.status_code == 200
    assert response.json()["dry_run"] is True

    after = client.get("/v1/admin/settings", headers=_auth("admin"), params={"level": "tenant"})
    assert after.json()["values"] == {}


def test_embedding_change_is_refused_with_reindex_required(client: TestClient) -> None:
    response = client.patch(
        "/v1/admin/settings",
        headers={**_auth("admin"), "If-Match": 'W/"rev-0"'},
        params={"level": "tenant"},
        json={"models.embedding": "openai:text-embedding-3-large"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reindex_required"


def test_unknown_key_is_reference_not_found(client: TestClient) -> None:
    response = client.patch(
        "/v1/admin/settings",
        headers={**_auth("admin"), "If-Match": 'W/"rev-0"'},
        params={"level": "tenant"},
        json={"not.a.real.key": 1},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reference_not_found"


# --- AC1: Read a level ------------------------------------------------------


def test_ac1_admin_reads_project_level(client: TestClient, app_service: _SettingsApp) -> None:
    _seed_cascade_fixture(app_service)
    response = client.get(
        "/v1/admin/settings", headers=_auth("admin"), params={"level": "project:cpm"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "level": "project:cpm",
        "revision": "rev_41",
        "values": {"chunking.max_tokens": 384},
    }
    assert response.headers["ETag"] == 'W/"rev-41"'


def test_ac1_editor_refused_at_project_level(client: TestClient, app_service: _SettingsApp) -> None:
    _seed_cascade_fixture(app_service)
    response = client.get(
        "/v1/admin/settings", headers=_auth("editor"), params={"level": "project:cpm"}
    )
    assert response.status_code == 403


def test_ac1_editor_allowed_at_connection_level(
    client: TestClient, app_service: _SettingsApp
) -> None:
    _seed_cascade_fixture(app_service)
    response = client.get(
        "/v1/admin/settings",
        headers=_auth("editor"),
        params={"level": "connection:cpm/cpm-tickets"},
    )
    assert response.status_code == 200


def test_ac1_nonexistent_project_is_reference_not_found(client: TestClient) -> None:
    response = client.get(
        "/v1/admin/settings", headers=_auth("admin"), params={"level": "project:nope"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reference_not_found"


def test_ac1_malformed_level_is_a_422_naming_the_field(client: TestClient) -> None:
    response = client.get(
        "/v1/admin/settings", headers=_auth("admin"), params={"level": "planet:x"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["details"] == [{"field": "level", "issue": body["error"]["message"]}]


def test_ac1_no_response_contains_a_secret(client: TestClient, app_service: _SettingsApp) -> None:
    _seed_cascade_fixture(app_service)
    for level in ("tenant", "project:cpm", "connection:cpm/cpm-tickets"):
        response = client.get("/v1/admin/settings", headers=_auth("admin"), params={"level": level})
        assert "secret" not in response.text.lower()


# --- AC2: Effective value and precedence ------------------------------------


def test_ac2_effective_cascades_connection_project_tenant_default(
    client: TestClient, app_service: _SettingsApp
) -> None:
    _seed_cascade_fixture(app_service)

    at_connection = client.get(
        "/v1/admin/settings",
        headers=_auth("editor"),
        params={"level": "connection:cpm/cpm-tickets", "effective": "true"},
    )
    assert at_connection.json()["values"]["chunking.max_tokens"] == {
        "value": 256,
        "origin": "connection",
    }

    at_sibling_connection = client.get(
        "/v1/admin/settings",
        headers=_auth("editor"),
        params={"level": "connection:cpm/cpm-docs", "effective": "true"},
    )
    assert at_sibling_connection.json()["values"]["chunking.max_tokens"] == {
        "value": 384,
        "origin": "project",
    }

    at_other_project = client.get(
        "/v1/admin/settings",
        headers=_auth("admin"),
        params={"level": "project:other", "effective": "true"},
    )
    assert at_other_project.json()["values"]["chunking.max_tokens"] == {
        "value": 512,
        "origin": "tenant",
    }


def test_ac2_a_key_set_nowhere_has_origin_default(
    client: TestClient, app_service: _SettingsApp
) -> None:
    _seed_cascade_fixture(app_service)
    response = client.get(
        "/v1/admin/settings",
        headers=_auth("admin"),
        params={"level": "tenant", "effective": "true"},
    )
    assert response.json()["values"]["ingestion.max_attempts"] == {"value": 3, "origin": "default"}
