"""start --wait and watch follow a Temporal run with the inline renderer."""

from __future__ import annotations

import json

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.workflow_control import AppResponse

# Every command below goes through the CLI's project gate; give each test its own
# project instead of inheriting whatever directory pytest was started from.
pytestmark = pytest.mark.usefixtures("cli_project")


class FollowService(MockAppService):
    def __init__(self) -> None:
        super().__init__()
        self.status_calls = 0
        self.result_calls: list[str] = []

    async def start_ingestion(self, **kwargs):  # type: ignore[override]
        assert kwargs["wait"] is False
        return AppResponse(
            True,
            {
                "run": {
                    "run_id": "run-42",
                    "tenant_id": kwargs["tenant_id"],
                    "connector_name": kwargs["connector_name"],
                },
                "workflow": {"workflow_id": "wf"},
            },
        )

    async def ingestion_status(self, run_id):
        self.status_calls += 1
        status = "RUNNING" if self.status_calls < 3 else "COMPLETED"
        processed = min(self.status_calls, 3)
        return AppResponse(
            True,
            {
                "status": {"run_id": run_id, "status": status},
                "execution_status": "running" if status == "RUNNING" else "completed",
                "progress": {"discovered": 3, "processed": processed, "published": processed},
            },
        )

    async def ingestion_result(self, run_id):
        self.result_calls.append(run_id)
        return AppResponse(
            True,
            {
                "result": {
                    "task_id": run_id,
                    "status": "completed",
                    "discovered": 3,
                    "published": 3,
                    "unchanged": 0,
                    "failed": 0,
                }
            },
        )


def _factory(services: list[FollowService]):
    def make() -> FollowService:
        services.append(FollowService())
        return services[-1]

    return make


def test_start_wait_follows_then_prints_the_result(monkeypatch, capsys) -> None:
    services: list[FollowService] = []
    monkeypatch.setattr(cli_runner, "runtime_app_service", _factory(services))
    monkeypatch.setattr("harborrag_app.cli.commands.ingest._FOLLOW_INTERVAL", 0.0)

    code = cli.main(["--no-color", "ingest", "start", "--connector-id", "ws", "--wait"])

    out = capsys.readouterr().out
    assert code == 0
    assert services[0].status_calls >= 3
    assert services[0].result_calls == ["run-42"]
    assert "Ingestion started" in out and "Ingestion completed" in out


def test_start_wait_json_keeps_the_single_envelope(monkeypatch, capsys) -> None:
    class JsonService(FollowService):
        async def start_ingestion(self, **kwargs):  # type: ignore[override]
            assert kwargs["wait"] is True
            return AppResponse(True, {"run": {"run_id": "r"}, "result": {"status": "completed"}})

    monkeypatch.setattr(cli_runner, "runtime_app_service", JsonService)

    assert cli.main(["ingest", "start", "--connector-id", "ws", "--wait", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["result"]["status"] == "completed"


def test_watch_runs_to_terminal_and_prints_the_result(monkeypatch, capsys) -> None:
    services: list[FollowService] = []
    monkeypatch.setattr(cli_runner, "runtime_app_service", _factory(services))

    code = cli.main(["--no-color", "ingest", "watch", "run-42", "--refresh", "0.25"])

    assert code == 0
    assert services[0].result_calls == ["run-42"]
    assert "COMPLETED" in capsys.readouterr().out


def test_watch_events_emits_ndjson(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", FollowService)

    assert cli.main(["ingest", "watch", "run-42", "--refresh", "0.25", "--events"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["event"] == "progress"
    assert lines[-1]["ok"] is True


def test_the_textual_dashboard_is_gone() -> None:
    import importlib.util

    assert importlib.util.find_spec("harborrag_app.cli.dashboard") is None
