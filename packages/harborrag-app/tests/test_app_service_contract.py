"""Application service and production CLI boundary tests."""

from __future__ import annotations

import json

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.services.base import BaseAppService


class BrokenService(BaseAppService):
    """Call abstract method bodies to verify their defensive behavior."""

    def health(self):
        return super().health()

    def ingest_once(self):
        return super().ingest_once()


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
    assert "Operations" in rendered
    assert "Ingestion" in rendered
    assert "sample-ingest" not in rendered


def test_cli_service_construction_failure_uses_stable_json(monkeypatch, capsys) -> None:
    def unavailable_service() -> MockAppService:
        raise RuntimeError("Temporal configuration is invalid")

    monkeypatch.setattr(cli_runner, "runtime_app_service", unavailable_service)

    assert cli.main(["doctor", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "data": {"error_type": "RuntimeError"},
        "error": "Temporal configuration is invalid",
        "ok": False,
    }
