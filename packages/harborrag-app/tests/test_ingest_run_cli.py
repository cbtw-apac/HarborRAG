"""`ingest run` executes inline, follows the task store, and reports the final result."""

from __future__ import annotations

import json

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner

# Every command below goes through the CLI's project gate; give each test its own
# project instead of inheriting whatever directory pytest was started from.
pytestmark = pytest.mark.usefixtures("cli_project")


def _service(final_status: str = "completed") -> type[MockAppService]:
    class Service(MockAppService):
        def __init__(self) -> None:
            super().__init__()
            self.direct_final_status = final_status

    return Service


def test_run_json_returns_the_final_result_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", _service())

    code = cli.main(["ingest", "run", "workspace", "--tenant", "t1", "--limit", "5", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["result"]["status"] == "completed"
    assert payload["data"]["result"]["discovered"] == 2


def test_run_records_the_request_it_made(monkeypatch, capsys) -> None:
    service_type = _service()
    instances: list[MockAppService] = []

    def factory() -> MockAppService:
        instances.append(service_type())
        return instances[-1]

    monkeypatch.setattr(cli_runner, "runtime_app_service", factory)

    cli.main(
        [
            "ingest",
            "run",
            "workspace",
            "--tenant",
            "t1",
            "--limit",
            "5",
            "--force-reprocess",
            "--run-id",
            "ingest-fixed",
            "--json",
        ]
    )

    call = instances[0].direct_runs[0]
    assert call["connector_name"] == "workspace"
    assert call["tenant_id"] == "t1"
    assert call["max_artifacts"] == 5
    assert call["force_reprocess"] is True
    assert call["run_id"] == "ingest-fixed"


def test_run_events_streams_ndjson_then_the_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", _service())

    code = cli.main(["ingest", "run", "workspace", "--events"])

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert lines[0]["event"] == "progress"
    assert lines[-1]["ok"] is True and "result" in lines[-1]["data"]


def test_run_exits_one_when_the_run_failed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", _service("failed"))

    assert cli.main(["ingest", "run", "workspace", "--json"]) == 1


def test_run_prints_a_summary_when_not_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", _service())

    assert cli.main(["--no-color", "ingest", "run", "workspace"]) == 0

    out = capsys.readouterr().out
    assert "Ingestion completed" in out
    assert "Discovered" in out
