"""Pause, resume, cancel and retry-failures routes answer the run; the start records its caller."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

import pytest
from app_test_connections import CONNECTION_KEY, JIRA_WRITE, connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_admin_connections import _SECRET, _token

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.domain.project import Project

_BASE = "/v1/admin/ingestions"
_VIEW = {
    "ingestion_id": "ing_9",
    "tenant_id": "acme",
    "project_id": "cpm",
    "connection_id": "cpm-tickets",
    "mode": "incremental",
    "status": "paused",
    "counters": {"discovered": 0, "processed": 0, "skipped": 0, "failed": 0},
    "stages": [],
    "error_category": None,
    "triggered_by": {"principal_type": "user", "principal_id": "u_huy"},
    "settings_revision": "",
    "workflow_id": "harborrag-source:ing_9",
    "run_id": "ing_9",
    "trace_id": "",
    "links": {"temporal": "", "grafana": "", "langfuse": ""},
    "created_at": None,
    "finished_at": None,
}


class _Operations:
    """Records the calls and answers with a fixed run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def act(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((kwargs["action"], kwargs))
        return {**_VIEW, "ingestion_id": kwargs["ingestion_id"]}

    async def retry_failures(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("retry-failures", kwargs))
        return {**_VIEW, "ingestion_id": "ing_new", "status": "queued"}


class _App(MockAppService):
    def __init__(self) -> None:
        super().__init__()
        self.ingestion_operations = _Operations()


@pytest.fixture
def app_service() -> _App:
    return _App()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, app_service: _App) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (app_service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {_token('editor', ['acme'])}"
        test_client.headers["X-Tenant-Id"] = "acme"
        yield test_client


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_a_control_action_answers_200_with_the_run(
    client: TestClient, app_service: _App, action: Literal["pause", "resume", "cancel"]
) -> None:
    response = client.post(f"{_BASE}/ing_9/{action}")

    assert response.status_code == 200
    assert response.json()["ingestion_id"] == "ing_9"
    assert response.json()["status"] == "paused"
    assert app_service.ingestion_operations.calls == [
        (
            action,
            {"tenant_id": "acme", "ingestion_id": "ing_9", "action": action, "actor": "u_huy"},
        )
    ]


def test_retry_failures_answers_202_with_the_new_run_and_its_location(
    client: TestClient, app_service: _App
) -> None:
    response = client.post(f"{_BASE}/ing_9/retry-failures", json={"document_ids": ["d1"]})

    assert response.status_code == 202
    assert response.json()["ingestion_id"] == "ing_new"
    assert response.headers["location"] == f"{_BASE}/ing_new"
    assert app_service.ingestion_operations.calls[0][1] == {
        "tenant_id": "acme",
        "ingestion_id": "ing_9",
        "actor": "u_huy",
        "actor_type": "user",
        "document_ids": ["d1"],
    }


def test_retry_failures_without_a_body_retries_every_failure(
    client: TestClient, app_service: _App
) -> None:
    assert client.post(f"{_BASE}/ing_9/retry-failures").status_code == 202
    assert app_service.ingestion_operations.calls[0][1]["document_ids"] == []


def test_the_restart_action_does_not_exist(client: TestClient) -> None:
    assert client.post(f"{_BASE}/ing_9/restart").status_code == 404


def test_a_reader_cannot_control_a_run(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {_token('reader', ['acme'])}"}

    assert client.post(f"{_BASE}/ing_9/pause", headers=headers).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_type", ["api_key", "user"])
async def test_a_sync_hands_the_caller_to_the_stored_run(actor_type: str) -> None:
    world = connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="seed")

    await world.service.sync(CONNECTION_KEY, actor="key_1", actor_type=actor_type)

    command = world.submitter.commands[-1]
    assert (command.actor, command.actor_type) == ("key_1", actor_type)
