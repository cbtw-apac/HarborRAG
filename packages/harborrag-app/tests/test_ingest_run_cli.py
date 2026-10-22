"""`ingest run` executes inline, follows the task store, and reports the final result."""

from __future__ import annotations

import asyncio
import json
import logging

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


def test_a_failed_progress_read_does_not_cancel_the_run(monkeypatch, capsys) -> None:
    finished: list[bool] = []

    class FlakyReader(MockAppService):
        async def get_task(self, task_id: str) -> dict[str, object]:
            raise RuntimeError("database is locked")

        async def run_ingestion(self, **kwargs):  # type: ignore[override]
            await asyncio.sleep(0.2)
            response = await super().run_ingestion(**kwargs)
            finished.append(True)
            return response

    monkeypatch.setattr(cli_runner, "runtime_app_service", FlakyReader)

    code = cli.main(["ingest", "run", "workspace", "--events"])

    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert finished == [True]
    assert code == 0
    assert last["ok"] is True and last["data"]["result"]["status"] == "completed"


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


def test_close_failure_does_not_replace_the_envelope(monkeypatch, capsys) -> None:
    class Service(_service()):
        async def aclose(self) -> None:
            raise ExceptionGroup("close", [RuntimeError("x")])

    monkeypatch.setattr(cli_runner, "runtime_app_service", Service)
    # Earlier tests leave a verbose level and a handler bound to a closed stderr behind.
    # The trace is DEBUG-only by design, and the warning must land on this test's stderr.
    monkeypatch.setenv("HARBORRAG_LOG_LEVEL", "WARNING")
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])
    monkeypatch.setattr(cli_runner.logger, "level", logging.NOTSET)

    code = cli.main(["ingest", "run", "workspace", "--tenant", "t1", "--json"])

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out.strip().splitlines()[-1])["ok"] is True
    assert "Traceback" not in captured.err


def _checkout(tmp_path, monkeypatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "connectors.yaml").write_text("version: 1\nconnectors: {}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    # conftest's autouse fixture sets a per-test sqlite URL; the fallback means "unset".
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)


def test_checkout_refuses_ingestion_on_the_sqlite_fallback(tmp_path, monkeypatch, capsys) -> None:
    _checkout(tmp_path, monkeypatch)
    built: list[int] = []

    def factory() -> MockAppService:
        built.append(1)
        return MockAppService()

    monkeypatch.setattr(cli_runner, "runtime_app_service", factory)

    code = cli.main(["ingest", "run", "workspace"])

    err = capsys.readouterr().err
    assert code == 1
    assert "HARBORRAG_CONTROL_DB_URL" in err and "harborrag init" in err
    assert built == []


def test_checkout_guard_stays_quiet_when_the_control_db_is_set(
    tmp_path, monkeypatch, capsys
) -> None:
    _checkout(tmp_path, monkeypatch)
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", "postgresql+asyncpg://u@db.example/harbor")
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["ingest", "run", "workspace"])

    assert "Refusing to ingest" not in capsys.readouterr().err


def test_ctrl_c_prints_the_run_id(monkeypatch, capsys) -> None:
    import asyncio

    class SlowRun(MockAppService):
        async def run_ingestion(self, **kwargs):  # type: ignore[override]
            await asyncio.sleep(30)
            raise AssertionError("the run should have been cancelled")

    def interrupted(self, until=None):
        raise asyncio.CancelledError

    monkeypatch.setattr(cli_runner, "runtime_app_service", SlowRun)
    monkeypatch.setattr("harborrag_app.cli.progress_view.LiveProgress.follow", interrupted)

    code = cli.main(["ingest", "run", "workspace", "--run-id", "run-7"])

    err = capsys.readouterr().err
    assert code == 130
    assert "Cancelled inline run run-7" in err


def test_tenant_defaults_from_environment(monkeypatch) -> None:
    instances: list[MockAppService] = []

    def factory() -> MockAppService:
        instances.append(_service()())
        return instances[-1]

    monkeypatch.setattr(cli_runner, "runtime_app_service", factory)
    monkeypatch.setenv("HARBORRAG_INGESTION_TENANT_ID", "acme")

    cli.main(["ingest", "run", "workspace", "--json"])

    assert instances[0].direct_runs[0]["tenant_id"] == "acme"


_SIGINT_SCRIPT = """
import asyncio, sys
from pathlib import Path
sys.path.insert(0, {tests_dir!r})
from app_test_fixtures import MockAppService
from harborrag_app.cli import main as cli
from harborrag_app.cli import runner

marker = Path({marker!r})

class SlowRun(MockAppService):
    async def run_ingestion(self, **kwargs):
        marker.write_text("started")
        await asyncio.sleep(60)

runner.runtime_app_service = SlowRun
sys.exit(cli.main(["ingest", "run", "workspace", "--run-id", "run-sig"]))
"""


@pytest.mark.smoke
def test_real_sigint_exits_130_and_names_the_run(cli_project, tmp_path) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    marker = tmp_path / "started"
    script = _SIGINT_SCRIPT.format(tests_dir=str(Path(__file__).parent), marker=str(marker))
    env = {**os.environ, "HARBORRAG_PROJECT": str(cli_project)}
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.exists():
            assert proc.poll() is None and time.monotonic() < deadline, "run never started"
            time.sleep(0.05)
        time.sleep(0.3)
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, err
    assert "Cancelled inline run run-sig" in err
