"""Contract tests for application-owned schedule management."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.dependencies import get_principal
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.errors import (
    ScheduleAlreadyExistsError,
    ScheduleInvalidError,
    ScheduleManagedByConfigError,
    ScheduleNotFoundAppError,
)
from harborrag_app.workflow_control.scheduling.models import ScheduleCommand
from harborrag_app.workflow_control.scheduling.service import ScheduleApplicationService
from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_runtime.config.errors import TemporalConfigurationError


class _ScheduleService:
    def __init__(self) -> None:
        self.schedule = {
            "schedule_id": "artifact-update",
            "workflow": "source_ingestion",
            "cron": "0 1 * * *",
            "interval_seconds": None,
            "timezone": "Asia/Ho_Chi_Minh",
            "overlap": "buffer_one",
            "catchup_window_seconds": 1800,
            "jitter_seconds": 30,
            "pause_on_failure": True,
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
        self.commands: list[object] = []
        self.update_error: Exception | None = None
        self.get_error: Exception | None = None

    async def create_schedule(self, command):
        self.commands.append(command)
        return self.schedule

    async def update_schedule(self, command):
        if self.update_error is not None:
            raise self.update_error
        self.commands.append(command)
        return self.schedule

    async def list_schedules(self, *, tenant_ids):
        return {"items": [self.schedule]}

    async def get_schedule(self, schedule_id):
        if self.get_error is not None:
            raise self.get_error
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


def _editor(client: TestClient, tenants: frozenset[str] = frozenset({"DEFAULT"})) -> None:
    client.app.dependency_overrides[get_principal] = lambda: Principal(
        subject="editor", role="editor", tenant_ids=tenants
    )


def test_create_schedule_defaults_to_the_default_tenant(
    client: TestClient, service: _ScheduleService
) -> None:
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
    assert service.commands[0].tenant_id == "DEFAULT"


def test_schedule_controls_are_exposed(client: TestClient, service: _ScheduleService) -> None:
    response = client.post("/v1/schedules/artifact-update/pause", json={"note": "maintenance"})

    assert response.status_code == 202
    assert service.actions == [("artifact-update", "pause")]


def test_schedule_create_rejects_another_tenant(
    client: TestClient, service: _ScheduleService
) -> None:
    _editor(client, frozenset({"ACME"}))

    response = client.post(
        "/v1/schedules",
        json={
            "schedule_id": "artifact-update",
            "workflow": "source_ingestion",
            "cron": "0 1 * * *",
            "source": {"connection_id": "workspace"},
        },
    )

    assert response.status_code == 403
    assert service.commands == []


@pytest.mark.parametrize("operation", ["get", "pause", "patch", "delete"])
def test_another_tenants_schedule_looks_missing(
    client: TestClient, service: _ScheduleService, operation: str
) -> None:
    _editor(client, frozenset({"ACME"}))

    if operation == "get":
        response = client.get("/v1/schedules/artifact-update")
    elif operation == "pause":
        response = client.post("/v1/schedules/artifact-update/pause", json={})
    elif operation == "patch":
        response = client.patch("/v1/schedules/artifact-update", json={"cron": "0 2 * * *"})
    else:
        response = client.delete("/v1/schedules/artifact-update")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCHEDULE_NOT_FOUND"
    assert service.commands == []
    assert service.actions == []


def test_editor_cannot_choose_a_source_scope(client: TestClient, service: _ScheduleService) -> None:
    _editor(client)

    response = client.post(
        "/v1/schedules",
        json={
            "schedule_id": "artifact-update",
            "workflow": "source_ingestion",
            "cron": "0 1 * * *",
            "source": {"connection_id": "workspace", "source_scope_id": "finance"},
        },
    )

    assert response.status_code == 403
    assert service.commands == []


def test_admin_can_choose_a_source_scope(client: TestClient, service: _ScheduleService) -> None:
    client.app.dependency_overrides[get_principal] = lambda: Principal(
        subject="admin", role="admin", tenant_ids=frozenset({"DEFAULT"})
    )

    response = client.post(
        "/v1/schedules",
        json={
            "schedule_id": "artifact-update",
            "workflow": "source_ingestion",
            "cron": "0 1 * * *",
            "source": {"connection_id": "workspace", "source_scope_id": "finance"},
        },
    )

    assert response.status_code == 201
    assert service.commands[0].source_scope_id == "finance"


def test_editor_cannot_patch_a_scoped_schedule(
    client: TestClient, service: _ScheduleService
) -> None:
    _editor(client)
    service.schedule["source"]["source_scope_id"] = "finance"

    response = client.patch("/v1/schedules/artifact-update", json={"cron": "0 2 * * *"})

    assert response.status_code == 403
    assert service.commands == []


def test_unsupported_overlap_is_rejected(client: TestClient, service: _ScheduleService) -> None:
    response = client.post(
        "/v1/schedules",
        json={
            "schedule_id": "artifact-update",
            "workflow": "source_ingestion",
            "cron": "0 1 * * *",
            "overlap": "cancel_other",
            "source": {"connection_id": "workspace"},
        },
    )

    assert response.status_code == 422
    assert service.commands == []


def test_schedule_reader_cannot_pause(client: TestClient, service: _ScheduleService) -> None:
    client.app.dependency_overrides[get_principal] = lambda: Principal(
        subject="reader", role="reader", tenant_ids=frozenset({"DEFAULT"})
    )

    response = client.post("/v1/schedules/artifact-update/pause", json={})

    assert response.status_code == 403
    assert service.actions == []


def test_config_owned_schedule_update_returns_conflict(
    client: TestClient, service: _ScheduleService
) -> None:
    service.update_error = ScheduleManagedByConfigError("Change the YAML configuration instead")

    response = client.patch("/v1/schedules/artifact-update", json={"cron": "0 2 * * *"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCHEDULE_MANAGED_BY_CONFIG"


def test_missing_schedule_returns_not_found(client: TestClient, service: _ScheduleService) -> None:
    service.get_error = ScheduleNotFoundAppError("Schedule was not found")

    response = client.get("/v1/schedules/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCHEDULE_NOT_FOUND"


def test_schedule_unpause_route_uses_temporal_term(
    client: TestClient, service: _ScheduleService
) -> None:
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


def test_patch_changes_only_the_fields_sent(client: TestClient, service: _ScheduleService) -> None:
    response = client.patch("/v1/schedules/artifact-update", json={"cron": "0 2 * * *"})

    assert response.status_code == 200
    command = service.commands[0]
    assert command == ScheduleCommand(
        schedule_id="artifact-update",
        workflow="source_ingestion",
        cron="0 2 * * *",
        interval_seconds=None,
        timezone="Asia/Ho_Chi_Minh",
        overlap="buffer_one",
        catchup_window_seconds=1800,
        jitter_seconds=30,
        pause_on_failure=True,
        tenant_id="DEFAULT",
        connection_id="workspace",
        source_scope_id=None,
        force_reprocess=False,
        paused=None,
        note=None,
    )


def test_patch_merges_nested_source_fields_and_clears_with_null(
    client: TestClient, service: _ScheduleService
) -> None:
    response = client.patch(
        "/v1/schedules/artifact-update",
        json={"jitter_seconds": None, "source": {"mode": "force"}, "paused": True},
    )

    assert response.status_code == 200
    command = service.commands[0]
    assert command.jitter_seconds is None
    assert command.force_reprocess is True
    assert command.connection_id == "workspace"
    assert command.cron == "0 1 * * *"
    assert command.paused is True


@pytest.mark.parametrize(
    "body",
    [{"cron": None}, {"schedule_id": "renamed"}, {"workflow": "source_ingestion"}],
)
def test_patch_rejects_invalid_results_and_immutable_fields(
    client: TestClient, service: _ScheduleService, body: dict[str, object]
) -> None:
    response = client.patch("/v1/schedules/artifact-update", json=body)

    assert response.status_code == 422
    assert service.commands == []


class _FailingSyncService(_ScheduleService):
    async def sync_declared_schedules(self):
        raise ConnectionError("temporal unreachable")


def test_api_starts_when_declared_schedule_sync_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_app, "_SCHEDULE_SYNC_RETRY_INTERVAL_SECONDS", 3600.0)
    service = _FailingSyncService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))

    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        assert test_client.get("/v1/schedules").status_code == 200


@pytest.mark.parametrize(
    ("first_error", "expected_calls"),
    [(ConnectionError("temporal unreachable"), 2), (ValueError("bad schedules.yaml"), 1)],
)
def test_declared_schedule_sync_retries_only_transient_failures(
    monkeypatch: pytest.MonkeyPatch, first_error: Exception, expected_calls: int
) -> None:
    monkeypatch.setattr(api_app, "_SCHEDULE_SYNC_RETRY_INTERVAL_SECONDS", 0.0)
    calls: list[int] = []

    async def sync_declared_schedules():
        calls.append(1)
        if len(calls) == 1:
            raise first_error
        return {"created": 0, "updated": 0, "deleted": 0, "failed": 0}

    app = SimpleNamespace(
        state=SimpleNamespace(
            app_service=SimpleNamespace(sync_declared_schedules=sync_declared_schedules)
        )
    )

    asyncio.run(api_app._sync_declared_schedules(app))

    assert len(calls) == expected_calls


def test_declared_sync_ignores_singular_schedule_file(tmp_path: Path) -> None:
    (tmp_path / "schedule.yaml").write_text("version: 1\nschedules: []\n")

    async def backend_provider():
        raise AssertionError("no schedule file is deployed, so the engine is not contacted")

    schedules = ScheduleApplicationService(
        SimpleNamespace(schedule_config_path=tmp_path / "schedules.yaml"),
        backend_provider=backend_provider,
        source_input_builder=lambda settings, submission: submission,
    )

    assert asyncio.run(schedules.sync_declared()) is None


def _schedules(tmp_path: Path, backend_provider) -> ScheduleApplicationService:
    return ScheduleApplicationService(
        SimpleNamespace(schedule_config_path=tmp_path / "schedules.yaml"),
        backend_provider=backend_provider,
        source_input_builder=lambda settings, submission: submission,
    )


def _command(schedule_id: str) -> ScheduleCommand:
    return ScheduleCommand(
        schedule_id=schedule_id,
        workflow="source_ingestion",
        cron="0 1 * * *",
        timezone="UTC",
        overlap="skip",
        tenant_id="DEFAULT",
        connection_id="workspace",
    )


def test_api_cannot_take_an_id_declared_in_config(tmp_path: Path) -> None:
    (tmp_path / "schedules.yaml").write_text(
        "version: 1\n"
        "schedules:\n"
        "  - id: nightly\n"
        "    workflow: source_ingestion\n"
        "    cron: '0 1 * * *'\n"
        "    source:\n"
        "      connection_id: workspace\n"
    )

    async def backend_provider():
        raise AssertionError("a reserved ID is rejected before the engine is contacted")

    with pytest.raises(ScheduleAlreadyExistsError, match="unavailable"):
        asyncio.run(_schedules(tmp_path, backend_provider).create(_command("nightly")))


def test_unconfigured_schedule_engine_is_unavailable_not_a_server_error(tmp_path: Path) -> None:
    async def backend_provider():
        raise TemporalConfigurationError("Temporal configuration is missing")

    with pytest.raises(HarborConnectionError):
        asyncio.run(_schedules(tmp_path, backend_provider).get("nightly"))


def test_invalid_schedule_stays_a_validation_error(tmp_path: Path) -> None:
    async def backend_provider():
        raise AssertionError("an invalid definition is rejected before the engine is contacted")

    invalid = replace(_command("nightly"), overlap="sometimes")

    with pytest.raises(ScheduleInvalidError):
        asyncio.run(_schedules(tmp_path, backend_provider).create(invalid))
