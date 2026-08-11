"""Application service and production CLI boundary tests."""

from __future__ import annotations

import json

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.workflow_control import BaseAppService
from harborrag_app.workflow_control.schemas import AppResponse


class BrokenService(BaseAppService):
    """Call abstract method bodies to verify their defensive behavior."""

    def health(self):
        return super().health()

    def ingest_once(self):
        return super().ingest_once()

    # Implement newly-abstract read methods by delegating to the
    # abstract base implementations to ensure they raise
    async def list_projects(self) -> AppResponse:
        return await super().list_projects()

    async def get_project(self, project_id: str) -> AppResponse:
        return await super().get_project(project_id)

    async def list_sources(self, project_id: str | None = None) -> AppResponse:
        return await super().list_sources(project_id)

    async def get_source(self, source_id: str) -> AppResponse:
        return await super().get_source(source_id)

    async def create_source(self, **kwargs: object) -> AppResponse:
        return await super().create_source(**kwargs)

    async def update_source(self, source_id: str, **kwargs: object) -> AppResponse:
        return await super().update_source(source_id, **kwargs)

    async def delete_source(self, source_id: str, **kwargs: object) -> AppResponse:
        return await super().delete_source(source_id, **kwargs)

    async def list_activity(self, limit: int = 50) -> AppResponse:
        return await super().list_activity(limit)

    async def get_settings(self) -> AppResponse:
        return await super().get_settings()

    async def get_metrics(self) -> AppResponse:
        return await super().get_metrics()


def test_app_service_abstract_methods_raise() -> None:
    service = BrokenService()

    with pytest.raises(NotImplementedError):
        service.health()
    with pytest.raises(NotImplementedError):
        service.ingest_once()


def test_doctor_uses_runtime_service_and_stable_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    assert cli.main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"]["diagnostics"]["runtime"]["ready"] is True


def test_typer_help_groups_commands_and_excludes_removed_sample(capsys) -> None:
    assert cli.main(["--help"]) == 0

    rendered = capsys.readouterr().out
    command_names = {command.name for command in cli.app.registered_commands}
    assert "Operations" in rendered
    assert "Ingestion" in rendered
    assert "sample-ingest" not in rendered
    assert "status" not in command_names


def test_cli_service_construction_failure_uses_stable_json(monkeypatch, capsys) -> None:
    def unavailable_service() -> MockAppService:
        raise RuntimeError("Temporal configuration is invalid")

    monkeypatch.setattr(cli_runner, "runtime_app_service", unavailable_service)

    assert cli.main(["doctor", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "data": {"error_type": "RuntimeError"},
        "error": "RuntimeError",
        "ok": False,
    }
