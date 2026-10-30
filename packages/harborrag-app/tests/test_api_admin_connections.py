"""HTTP contract of the connection, project and source-type routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from app_test_connections import ConnectionWorld, connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.projects import ProjectService
from harborrag_core.domain.project import Project

_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
_PATH = "/v1/admin/projects/cpm/connections/cpm-tickets"
_ROUTE = "/v1/admin/projects/{project_id}/connections/{connection_id}"
_BODY = {
    "source_type": "jira",
    "name": "CPM tickets",
    "credentials": {
        "base_url": "https://acme.atlassian.net",
        "email": "bot@acme.com",
        "api_token": "ATATT3xFfGF0-sentinel-9f2c",
    },
    "config": {"jql": "project = CPM"},
}


class _ConnectionApp(MockAppService):
    """Mock app service carrying real connection and project services over fakes."""

    def __init__(self, world: ConnectionWorld) -> None:
        super().__init__()
        self.world = world
        self.connection_service = world.service
        self.project_service = ProjectService(
            control_plane=lambda: world.control_plane,
            runs=world.service._runs,
            connections=world.service,
        )


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
def app_service() -> _ConnectionApp:
    return _ConnectionApp(
        connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, app_service: _ConnectionApp) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (app_service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {_token('admin', ['acme'])}"
        yield test_client


def test_source_types_publish_every_v2_type(client: TestClient) -> None:
    response = client.get("/v1/admin/source-types")
    jira = next(item for item in response.json()["items"] if item["source_type"] == "jira")

    assert response.status_code == 200
    assert {item["source_type"] for item in response.json()["items"]} == {
        "jira",
        "confluence",
        "sharepoint",
        "local_file",
        "github",
    }
    assert jira["auth_methods"][0]["schema"]["properties"]["api_token"]["writeOnly"] is True
    assert jira["ui"]["api_token"]["widget"] == "secret"
    files = client.get("/v1/admin/source-types", params={"category": "files"}).json()["items"]
    assert {item["source_type"] for item in files} == {"sharepoint", "local_file"}


def test_three_calls_add_a_jira_connection(client: TestClient, app_service: _ConnectionApp) -> None:
    created = client.put(_PATH, json={**_BODY, "sync_now": True})

    body = created.json()
    assert created.status_code == 201
    assert created.headers["ETag"] == 'W/"rev-1"'
    assert body["ref"] == "cpm/cpm-tickets"
    assert body["credentials"]["api_token"]["from"] == "stored"
    assert body["credentials"]["api_token"]["last4"] == "9f2c"
    assert body["created_by"] == body["updated_by"] == "u_huy"
    assert body["ingestion"]["id"] == "ing_1"
    assert "sentinel" not in created.text

    read = client.get(_PATH)
    assert read.status_code == 200 and read.headers["ETag"] == 'W/"rev-1"'
    assert "sentinel" not in read.text

    overwritten = client.put(_PATH, json=_BODY, headers={"If-Match": 'W/"rev-1"'})
    assert overwritten.status_code == 200 and overwritten.headers["ETag"] == 'W/"rev-2"'


def test_put_errors_follow_the_spec(client: TestClient) -> None:
    client.put(_PATH, json=_BODY)

    stale = client.put(_PATH, json=_BODY, headers={"If-Match": 'W/"rev-9"'})
    exists = client.put(_PATH, json=_BODY, headers={"If-None-Match": "*"})
    other_type = client.put(
        _PATH, json={**_BODY, "source_type": "confluence", "config": {"space_key": "ENG"}}
    )
    bad_field = client.put(
        _PATH, json={**_BODY, "credentials": {**_BODY["credentials"], "base_url": "nope"}}
    )
    unknown_type = client.put(_PATH, json={**_BODY, "source_type": "notion"})
    no_project = client.put("/v1/admin/projects/missing/connections/x", json=_BODY)
    bad_id = client.put("/v1/admin/projects/cpm/connections/Bad%20Id", json=_BODY)

    assert (stale.status_code, stale.json()["error"]["code"]) == (412, "precondition_failed")
    assert (exists.status_code, exists.json()["error"]["code"]) == (412, "precondition_failed")
    assert (other_type.status_code, other_type.json()["error"]["code"]) == (409, "immutable_field")
    assert (bad_field.status_code, bad_field.json()["error"]["code"]) == (422, "validation_error")
    assert bad_field.json()["error"]["details"] == [
        {"field": "credentials.base_url", "issue": "must be an absolute http(s) URL"}
    ]
    assert (unknown_type.status_code, unknown_type.json()["error"]["code"]) == (
        422,
        "validation_error",
    )
    assert unknown_type.json()["error"]["details"][0]["field"] == "source_type"
    assert (no_project.status_code, no_project.json()["error"]["code"]) == (404, "not_found")
    assert bad_id.status_code == 422
    assert stale.json()["error"]["trace_id"]


def test_list_test_sync_and_delete(client: TestClient, app_service: _ConnectionApp) -> None:
    client.put(_PATH, json=_BODY)

    listed = client.get("/v1/admin/connections", params={"project_id": "cpm", "limit": 1})
    tested = client.post(
        f"{_PATH}/test", json={"source_type": "local_file", "config": {"source_path": "/nope"}}
    )
    synced = client.post(f"{_PATH}/sync", json={"mode": "full"})
    app_service.world.runs.active = app_service.world.runs.tasks["ing_1"]
    again = client.post(f"{_PATH}/sync", json={})
    busy_delete = client.delete(_PATH)
    app_service.world.runs.active = None
    deleted = client.delete(_PATH, params={"purge": "true"})

    assert listed.status_code == 200 and listed.json()["next_cursor"] is None
    assert [item["ref"] for item in listed.json()["items"]] == ["cpm/cpm-tickets"]
    assert tested.status_code == 200 and tested.json()["ok"] is False
    assert tested.json()["error_category"] == "unreachable"
    assert synced.status_code == 202
    assert synced.headers["Location"] == "/v1/admin/ingestions/ing_1"
    assert synced.json()["mode"] == "full"
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_running")
    assert again.json()["error"]["details"] == {"ingestion_id": "ing_1"}
    assert (busy_delete.status_code, busy_delete.json()["error"]["code"]) == (
        409,
        "already_running",
    )
    assert deleted.status_code == 202 and deleted.json()["job_id"].startswith("job_")


def test_projects_and_reader_chips(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, app_service: _ConnectionApp
) -> None:
    created = client.put("/v1/admin/projects/hr", json={"name": "HR"})
    updated = client.put(
        "/v1/admin/projects/hr", json={"name": "HR team"}, headers={"If-Match": 'W/"rev-1"'}
    )
    listed = client.get("/v1/admin/projects")
    client.put(_PATH, json=_BODY)
    in_use = client.delete("/v1/admin/projects/cpm")
    empty = client.delete("/v1/admin/projects/hr")
    reader = {"Authorization": f"Bearer {_token('reader', ['acme'])}"}
    chips = client.get("/v1/projects/cpm/connections", headers=reader)
    forbidden = client.get("/v1/admin/projects", headers=reader)

    assert created.status_code == 201 and created.headers["ETag"] == 'W/"rev-1"'
    assert updated.status_code == 200 and updated.json()["name"] == "HR team"
    assert [item["id"] for item in listed.json()["items"]] == ["cpm", "hr"]
    assert (in_use.status_code, in_use.json()["error"]["code"]) == (409, "in_use")
    assert in_use.json()["error"]["details"] == {"connections": ["cpm/cpm-tickets"]}
    assert empty.status_code == 204
    assert chips.status_code == 200 and chips.json()["items"][0]["documents"] == 7
    assert (forbidden.status_code, forbidden.json()["error"]["code"]) == (403, "forbidden")


def test_the_tenant_comes_from_the_credential_or_x_tenant_id(
    client: TestClient,
) -> None:
    several = {"Authorization": f"Bearer {_token('admin', ['acme', 'other'])}"}

    missing = client.get("/v1/admin/connections", headers=several)
    picked = client.get("/v1/admin/connections", headers={**several, "X-Tenant-Id": "acme"})
    foreign = client.get("/v1/admin/connections", headers={"X-Tenant-Id": "other"})

    assert (missing.status_code, missing.json()["error"]["code"]) == (400, "tenant_required")
    assert picked.status_code == 200
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (403, "tenant_forbidden")


@pytest.mark.parametrize(
    ("method", "path", "successor"),
    [
        ("get", "/v1/connections", "/v1/admin/connections"),
        ("get", "/api/v1/sources", "/v1/admin/connections"),
        (
            "post",
            "/v1/ingestions",
            "/v1/admin/projects/{project_id}/connections/{connection_id}/sync",
        ),
    ],
)
def test_old_routes_announce_their_successor(
    client: TestClient, method: str, path: str, successor: str
) -> None:
    kwargs: dict[str, Any] = {"json": {"connection_id": "jira-main"}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)

    assert response.headers["Deprecation"] == "true"
    assert response.headers["Sunset"] == "Sun, 07 Feb 2027 00:00:00 GMT"
    assert response.headers["Link"] == f'<{successor}>; rel="successor-version"'


def test_listing_ingestions_is_not_deprecated(client: TestClient) -> None:
    assert "Deprecation" not in client.get("/v1/ingestions").headers


def test_openapi_documents_the_connection_routes(client: TestClient) -> None:
    schema = client.app.openapi()  # type: ignore[attr-defined]
    paths = schema["paths"]

    assert set(
        paths[_PATH.replace("cpm-tickets", "{connection_id}").replace("cpm", "{project_id}")]
    ) == {
        "get",
        "put",
        "delete",
    }
    assert "post" in paths["/v1/admin/projects/{project_id}/connections/{connection_id}/test"]
    assert "post" in paths["/v1/admin/projects/{project_id}/connections/{connection_id}/sync"]
    assert paths["/v1/connections"]["get"]["deprecated"] is True
    assert paths["/v1/ingestions"]["post"]["deprecated"] is True
    assert paths["/api/v1/sources"]["post"]["deprecated"] is True


def test_local_connections_stay_inside_the_source_root(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("HARBORRAG_LOCAL_SOURCE_ROOT", str(tmp_path))
    body = {"source_type": "local_file", "credentials": {}, "config": {"source_path": "/proc/self"}}

    refused = client.put("/v1/admin/projects/cpm/connections/files", json=body)
    accepted = client.put(
        "/v1/admin/projects/cpm/connections/files",
        json={**body, "config": {"source_path": str(tmp_path)}},
    )

    assert (refused.status_code, refused.json()["error"]["code"]) == (422, "validation_error")
    assert refused.json()["error"]["details"][0]["field"] == "config.source_path"
    assert accepted.status_code == 201


def test_every_swagger_example_creates_a_connection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARBORRAG_LOCAL_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_SOURCE_PATH", raising=False)
    put = client.app.openapi()["paths"][_ROUTE]["put"]  # type: ignore[attr-defined]
    examples = put["requestBody"]["content"]["application/json"]["examples"]

    created = {
        name: client.put(
            f"/v1/admin/projects/cpm/connections/{name.replace('_', '-')}", json=ex["value"]
        )
        for name, ex in examples.items()
    }

    assert len(examples) == 10
    assert {name: response.status_code for name, response in created.items()} == dict.fromkeys(
        examples, 201
    )
    assert not any(example["value"]["sync_now"] for example in examples.values())


def test_test_route_examples_are_the_put_bodies_without_sync_now(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    put = paths[_ROUTE]["put"]["requestBody"]["content"]["application/json"]["examples"]
    test = paths[f"{_ROUTE}/test"]["post"]["requestBody"]["content"]["application/json"]

    assert {name: example["value"] for name, example in test["examples"].items()} == {
        name: {key: value for key, value in example["value"].items() if key != "sync_now"}
        for name, example in put.items()
    }
    assert paths[_ROUTE]["put"]["summary"] == "Create or replace a connection"


def test_a_project_named_by_its_display_name_points_at_its_id(client: TestClient) -> None:
    response = client.put("/v1/admin/projects/CPM/connections/cpm-tickets", json=_BODY)

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "project 'CPM' not found; did you mean 'cpm'?"
