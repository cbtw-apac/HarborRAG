"""Contract tests for application-owned schedule management."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


class _ScheduleService:
    def __init__(self) -> None:
        self.schedule = {
            "schedule_id": "artifact-update",
            "workflow": "source_ingestion",
            "cron": "0 1 * * *",
            "timezone": "UTC",
            "overlap": "skip",
            "paused": False,
            "note": None,
            "managed_by": "api",
            "source": {
                "tenant": "DEFAULT",
                "connection_id": "workspace",
                "source_scope_id": None,
                "mode": "incremental",
            },
            "next_run_times": [],
            "recent_runs": [],
            "total_runs": 0,
            "created_at": None,
            "updated_at": None,
        }
        self.actions: list[tuple[str, str]] = []

    async def create_schedule(self, command):
        return self.schedule

    async def update_schedule(self, command):
        return self.schedule

    async def list_schedules(self, *, tenant_ids):
        return {"items": [self.schedule]}

    async def get_schedule(self, schedule_id):
        return self.schedule

    async def pause_schedule(self, schedule_id, *, note):
        self.actions.append((schedule_id, "pause"))
        return {"schedule_id": schedule_id, "message": "Schedule paused"}

    async def unpause_schedule(self, schedule_id, *, note):
        self.actions.append((schedule_id, "unpause"))
        return {"schedule_id": schedule_id, "message": "Schedule unpaused"}

    async def trigger_schedule(self, schedule_id):
        self.actions.append((schedule_id, "trigger"))
        return {"schedule_id": schedule_id, "message": "Schedule run requested"}

    async def backfill_schedule(self, schedule_id, *, start_at, end_at):
        self.actions.append((schedule_id, "backfill"))
        return {"schedule_id": schedule_id, "message": "Schedule backfill requested"}

    async def delete_schedule(self, schedule_id):
        self.actions.append((schedule_id, "delete"))
        return {"schedule_id": schedule_id, "message": "Schedule deleted"}


@pytest.fixture
def service() -> _ScheduleService:
    return _ScheduleService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: _ScheduleService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def test_create_schedule_is_tenant_scoped(client: TestClient) -> None:
    response = client.post(
        "/v1/schedules",
        json={
            "schedule_id": "artifact-update",
            "workflow": "source_ingestion",
            "cron": "0 1 * * *",
            "source": {"connection_id": "workspace"},
        },
    )

    assert response.status_code == 201
    assert response.json()["schedule_id"] == "artifact-update"


def test_schedule_controls_are_exposed(client: TestClient, service: _ScheduleService) -> None:
    response = client.post("/v1/schedules/artifact-update/pause", json={"note": "maintenance"})

    assert response.status_code == 202
    assert service.actions == [("artifact-update", "pause")]


def test_schedule_unpause_route_uses_temporal_term(client: TestClient, service: _ScheduleService) -> None:
    response = client.post(
        "/v1/schedules/artifact-update/unpause",
        json={"note": "maintenance complete"},
    )

    assert response.status_code == 202
    assert service.actions == [("artifact-update", "unpause")]
    assert client.post("/v1/schedules/artifact-update/resume", json={}).status_code == 404


def test_schedule_id_is_required(client: TestClient) -> None:
    response = client.post(
        "/v1/schedules",
        json={"workflow": "source_ingestion", "cron": "0 1 * * *", "source": {}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"
