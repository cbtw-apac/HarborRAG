"""``POST /v1/admin/ingestions`` starts a run that ``Location`` reads; connections only store."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace

import pytest
from app_test_connections import ConnectionWorld, connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_admin_connections import _BODY, _PATH, _SECRET, _token

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.dependencies import get_principal
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.projects import ProjectService
from harborrag_core.domain.project import Project

_START = "/v1/admin/ingestions"
_TARGET = {"project_id": "cpm", "connection_id": "cpm-tickets"}


class _App(MockAppService):
    def __init__(self, world: ConnectionWorld) -> None:
        super().__init__()
        self.world = world
        self.connection_service = world.service
        self.ingestion_operations = world.operations
        self.project_service = ProjectService(
            control_plane=lambda: world.control_plane,
            runs=world.service._runs,
            connections=world.service,
        )


@pytest.fixture
def app_service() -> _App:
    return _App(
        connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, app_service: _App) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (app_service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {_token('editor', ['acme'])}"
        assert test_client.put(_PATH, json=_BODY).status_code == 201
        yield test_client


def test_starting_an_ingestion_answers_202_with_its_location(client: TestClient) -> None:
    response = client.post(_START, json={**_TARGET, "mode": "force"})

    assert response.status_code == 202
    body = response.json()
    assert body["ingestion_id"] == "ing_1"
    assert response.headers["location"] == f"/v1/admin/ingestions/{body['ingestion_id']}"


def test_a_start_by_an_api_key_records_it_as_the_trigger(client: TestClient) -> None:
    key = Principal(
        subject="key_1", role="editor", tenant_ids=frozenset({"acme"}), token_kind="api_key"
    )
    client.app.dependency_overrides[get_principal] = lambda: key  # type: ignore[attr-defined]

    started = client.post(_START, json=_TARGET)

    assert started.json()["triggered_by"] == {"principal_type": "api_key", "principal_id": "key_1"}
    assert (
        client.get(started.headers["location"]).json()["triggered_by"]
        == started.json()["triggered_by"]
    )


def test_the_location_header_reads_the_started_run(client: TestClient) -> None:
    started = client.post(_START, json=_TARGET)

    followed = client.get(started.headers["location"])

    assert followed.status_code == 200
    assert followed.json() == started.json()
    assert set(started.json()["links"]) == {"temporal", "grafana", "langfuse"}


def test_an_empty_mode_means_incremental(client: TestClient, app_service: _App) -> None:
    response = client.post(_START, json=_TARGET)

    assert response.status_code == 202
    assert app_service.world.submitter.commands[-1].force_reprocess is False


def test_a_second_start_while_one_runs_is_refused(client: TestClient, app_service: _App) -> None:
    client.post(_START, json=_TARGET)
    app_service.world.runs.active = app_service.world.runs.tasks["ing_1"]

    response = client.post(_START, json=_TARGET)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_running"
    assert response.json()["error"]["details"] == {"ingestion_id": "ing_1"}


@pytest.mark.parametrize(
    ("target", "field"),
    [
        ({"project_id": "missing", "connection_id": "cpm-tickets"}, "project_id"),
        ({"project_id": "cpm", "connection_id": "missing"}, "connection_id"),
    ],
)
def test_an_unknown_reference_names_its_field(
    client: TestClient, target: dict[str, str], field: str
) -> None:
    response = client.post(_START, json=target)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reference_not_found"
    assert response.json()["error"]["details"] == {"field": field}


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"project_id": "cpm"}, "connection_id"),
        ({**_TARGET, "mode": "full"}, "mode"),
        ({**_TARGET, "extra": True}, "extra"),
    ],
)
def test_a_bad_body_is_a_validation_error(
    client: TestClient, body: dict[str, object], field: str
) -> None:
    response = client.post(_START, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert [item["field"] for item in response.json()["error"]["details"]] == [field]


def test_a_connection_that_fails_its_schema_is_refused_with_its_fields(
    client: TestClient, app_service: _App
) -> None:
    connections = app_service.world.control_plane.connections
    stored = asyncio.run(
        connections.get(tenant_id="acme", project_id="cpm", connection_id="cpm-tickets")
    )
    assert stored is not None
    credentials = {k: v for k, v in stored.credentials.items() if k != "email"}
    asyncio.run(connections.put(replace(stored, credentials=credentials, status="error")))

    response = client.post(_START, json=_TARGET)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"] == [
        {"field": "credentials.email", "issue": "required"}
    ]
    assert app_service.world.submitter.commands == []


def test_a_scoped_run_is_not_supported_yet(client: TestClient) -> None:
    response = client.post(_START, json={**_TARGET, "item_refs": ["CPM-1"]})

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_supported"


def test_a_reader_cannot_start_an_ingestion(client: TestClient) -> None:
    response = client.post(
        _START, json=_TARGET, headers={"Authorization": f"Bearer {_token('reader', ['acme'])}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_the_connection_sync_route_is_gone(client: TestClient) -> None:
    response = client.post(f"{_PATH}/sync", json={})

    assert response.status_code == 404
    assert (
        "/v1/admin/projects/{project_id}/connections/{connection_id}/sync"
        not in (
            client.app.openapi()["paths"]  # type: ignore[attr-defined]
        )
    )


def test_saving_a_connection_starts_nothing(client: TestClient, app_service: _App) -> None:
    refused = client.put(_PATH, json={**_BODY, "sync_now": True})
    saved = client.put(_PATH, json={**_BODY, "name": "Renamed"})

    assert refused.status_code == 422
    assert [item["field"] for item in refused.json()["error"]["details"]] == ["sync_now"]
    assert saved.status_code == 200
    assert "ingestion" not in saved.json() and "sync_error" not in saved.json()
    assert app_service.world.submitter.commands == []
