"""Application service and production CLI boundary tests."""

from __future__ import annotations

import json

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.cli.doctor import probes
from harborrag_app.workflow_control import BaseAppService

# Every command below goes through the CLI's project gate; give each test its own
# project instead of inheriting whatever directory pytest was started from.
pytestmark = pytest.mark.usefixtures("cli_project")


class BrokenService(BaseAppService):
    """Call abstract method bodies to verify their defensive behavior."""

    def health(self):
        return super().health()


def test_app_service_abstract_methods_raise() -> None:
    with pytest.raises(NotImplementedError):
        BrokenService().health()


def _offline(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    for probe in ("tcp_reachable", "http_ok", "redis_ping"):
        monkeypatch.setattr(probes, probe, lambda *a, **k: "connection refused")


def test_doctor_uses_runtime_service_and_stable_json(monkeypatch, capsys, tmp_path) -> None:
    _offline(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    # An empty directory has no catalogs, so required checks fail and the exit code is 1.
    assert cli.main(["doctor", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "checks" in payload["data"]
    assert payload["data"]["diagnostics"]["runtime"]["ready"] is True


def test_typer_help_groups_commands_and_excludes_removed_sample(capsys) -> None:
    assert cli.main(["--help"]) == 0

    rendered = capsys.readouterr().out
    command_names = {command.name for command in cli.app.registered_commands}
    assert "Operations" in rendered
    assert "Resources" in rendered
    assert "sample-ingest" not in rendered
    assert "status" not in command_names


def test_cli_service_construction_failure_uses_stable_json(monkeypatch, capsys, tmp_path) -> None:
    def unavailable_service() -> MockAppService:
        raise RuntimeError("Temporal configuration is invalid")

    _offline(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_runner, "runtime_app_service", unavailable_service)

    assert cli.main(["doctor", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    control = next(c for c in payload["data"]["checks"] if c["name"] == "control plane")
    assert control["status"] == "fail"
    assert "RuntimeError" in control["detail"]
    assert "diagnostics" not in payload["data"]
