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


def test_parse_filters_accepts_a_json_object() -> None:
    from harborrag_app.cli.commands.ingest_run import parse_filters

    assert parse_filters('{"space": "ENG"}') == {"space": "ENG"}
    assert parse_filters("{}") == {}


def test_parse_filters_rejects_a_non_object() -> None:
    from harborrag_app.cli.commands.ingest_run import parse_filters

    with pytest.raises(ValueError, match="must encode a JSON object"):
        parse_filters("[1, 2]")


def test_parse_filters_reports_bad_json_in_the_options_own_terms() -> None:
    from harborrag_app.cli.commands.ingest_run import parse_filters

    with pytest.raises(ValueError, match="--filters-json is not valid JSON"):
        parse_filters("{oops")


def test_run_rejects_malformed_filters_before_building_a_service(monkeypatch, capsys) -> None:
    """A usage error must cost nothing and print no traceback.

    Exit 2 is Click's usage-error code, the same one `--provider nope` returns.
    """

    instances: list[MockAppService] = []

    def factory() -> MockAppService:
        instances.append(MockAppService())
        return instances[-1]

    monkeypatch.setattr(cli_runner, "runtime_app_service", factory)

    assert cli.main(["ingest", "run", "workspace", "--filters-json", "{oops"]) == 2

    assert instances == []  # never built the service
    assert "Traceback" not in capsys.readouterr().err


def test_run_forwards_every_option_it_accepts(monkeypatch) -> None:
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
            "--connection-id",
            "conn-1",
            "--source-scope-id",
            "scope-1",
            "--pattern",
            "*.md",
            "--no-recursive",
            "--updated-after",
            "2026-01-01T00:00:00Z",
            "--no-attachments",
            "--filters-json",
            '{"space": "ENG"}',
            "--json",
        ]
    )

    call = instances[0].direct_runs[0]
    assert call["connection_id"] == "conn-1"
    assert call["source_scope_id"] == "scope-1"
    assert call["pattern"] == "*.md"
    assert call["recursive"] is False
    assert call["updated_after"] == "2026-01-01T00:00:00Z"
    assert call["include_attachments"] is False
    assert call["filters"] == {"space": "ENG"}


@pytest.mark.parametrize("status", ["partial", "cancelled"])
def test_run_exits_one_when_the_run_did_not_complete(status, monkeypatch, capsys) -> None:
    """IngestionTaskState also carries PARTIAL and CANCELLED; neither is a success."""

    monkeypatch.setattr(cli_runner, "runtime_app_service", _service(status))

    assert cli.main(["ingest", "run", "workspace", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["data"]["result"]["status"] == status
