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
    """Submission and the wait are separate calls, but stdout still gets one envelope."""

    services: list[FollowService] = []
    monkeypatch.setattr(cli_runner, "runtime_app_service", _factory(services))

    argv = ["ingest", "start", "--connector-id", "ws", "--run-id", "run-42", "--wait", "--json"]
    assert cli.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert set(payload["data"]) == {"run", "workflow", "result"}
    assert payload["data"]["run"]["run_id"] == "run-42"
    assert payload["data"]["result"]["status"] == "completed"
    assert services[0].result_calls == ["run-42"]


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

    dashboard = importlib.util.find_spec("harborrag_app.cli.dashboard")
    assert dashboard is None or dashboard.loader is None


def test_watch_refuses_an_unusable_control_plane(monkeypatch, capsys) -> None:
    """`watch` polls run state, so it needs the same gate as `ingest status` and `wait`."""

    class DegradedControlPlane(FollowService):
        def health(self):
            return AppResponse(False, {}, "control-plane migrations failed")

    monkeypatch.setattr(cli_runner, "runtime_app_service", DegradedControlPlane)

    assert cli.main(["ingest", "watch", "run-42", "--refresh", "0.25", "--events"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["data"]["error_type"] == "ControlPlaneUnavailable"


def test_run_reports_an_escaping_service_error_as_the_envelope(monkeypatch, capsys) -> None:
    """`ingest run` bypasses runner._invoke, so it owns the error boundary itself."""

    class Exploding(MockAppService):
        async def run_ingestion(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("postgres://harbor:hunter2@db/harborrag went away")

    monkeypatch.setattr(cli_runner, "runtime_app_service", Exploding)

    assert cli.main(["ingest", "run", "workspace", "--json"]) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["data"]["error_type"] == "RuntimeError"
    assert "hunter2" not in captured.out
    assert "Traceback" not in captured.err


def test_run_does_not_orphan_the_ingestion_when_progress_explodes(monkeypatch, capsys) -> None:
    """A non-ProgressUnavailable escape from follow() must cancel the scheduled run.

    Otherwise the service closes underneath a pending coroutine and Python reports
    `Task exception was never retrieved` after the CLI has already printed.
    """

    import asyncio

    started = asyncio.Event()

    class SlowRun(MockAppService):
        async def run_ingestion(self, **kwargs):  # type: ignore[override]
            started.set()
            await asyncio.sleep(30)
            raise AssertionError("the run should have been cancelled")

    def exploding_follow(self, until=None):
        raise RuntimeError("the renderer blew up")

    monkeypatch.setattr(cli_runner, "runtime_app_service", SlowRun)
    monkeypatch.setattr("harborrag_app.cli.progress_view.LiveProgress.follow", exploding_follow)

    assert cli.main(["ingest", "run", "workspace"]) == 1
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("status", ["partial", "cancelled"])
def test_watch_exits_one_when_the_durable_run_did_not_complete(status, monkeypatch, capsys) -> None:
    """`watch` and `start --wait` share `ingest run`'s rule: only completed is success."""

    class Unfinished(FollowService):
        async def ingestion_result(self, run_id):
            return AppResponse(True, {"result": {"task_id": run_id, "status": status}})

    monkeypatch.setattr(cli_runner, "runtime_app_service", Unfinished)

    assert cli.main(["ingest", "watch", "run-42", "--refresh", "0.25", "--events"]) == 1
    assert (
        json.loads(capsys.readouterr().out.splitlines()[-1])["data"]["result"]["status"] == status
    )


@pytest.mark.parametrize("status", ["partial", "failed", "cancelled"])
def test_wait_exits_one_when_the_durable_run_did_not_complete(status, monkeypatch, capsys) -> None:
    class Unfinished(FollowService):
        async def ingestion_result(self, run_id):
            return AppResponse(True, {"result": {"task_id": run_id, "status": status}})

    monkeypatch.setattr(cli_runner, "runtime_app_service", Unfinished)

    assert cli.main(["ingest", "wait", "run-42", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["data"]["result"]["status"] == status


@pytest.mark.parametrize("status", ["partial", "failed", "cancelled"])
def test_start_wait_json_exits_one_when_the_run_did_not_complete(
    status, monkeypatch, capsys
) -> None:
    class Unfinished(FollowService):
        async def ingestion_result(self, run_id):
            return AppResponse(True, {"result": {"task_id": run_id, "status": status}})

    monkeypatch.setattr(cli_runner, "runtime_app_service", Unfinished)

    assert cli.main(["ingest", "start", "--connector-id", "ws", "--wait", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["data"]["result"]["status"] == status


def test_watch_ctrl_c_leaves_run_running_with_id(monkeypatch, capsys) -> None:
    import asyncio

    def interrupted(self, until=None):
        raise asyncio.CancelledError

    monkeypatch.setattr(cli_runner, "runtime_app_service", FollowService)
    monkeypatch.setattr("harborrag_app.cli.progress_view.LiveProgress.follow", interrupted)

    code = cli.main(["ingest", "watch", "run-42"])

    err = capsys.readouterr().err
    assert code == 130
    assert "run-42" in err and "harborrag ingest watch run-42" in err


def test_start_wait_ctrl_c_names_the_started_run(monkeypatch, capsys) -> None:
    import asyncio

    def interrupted(self, until=None):
        raise asyncio.CancelledError

    monkeypatch.setattr(cli_runner, "runtime_app_service", FollowService)
    monkeypatch.setattr("harborrag_app.cli.progress_view.LiveProgress.follow", interrupted)

    code = cli.main(["ingest", "start", "--connector-id", "ws", "--wait"])

    assert code == 130
    assert "harborrag ingest watch run-42" in capsys.readouterr().err


def test_wait_timeout_exits_three_and_names_the_run(monkeypatch, capsys) -> None:
    import asyncio

    class Slow(FollowService):
        async def ingestion_result(self, run_id):
            await asyncio.sleep(5)
            return await super().ingestion_result(run_id)

    monkeypatch.setattr(cli_runner, "runtime_app_service", Slow)

    code = cli.main(["ingest", "wait", "run-42", "--timeout", "1"])

    err = capsys.readouterr().err
    assert code == 3
    assert "run-42" in err and "it keeps running" in err
    assert "harborrag ingest watch run-42" in err


def test_watch_ends_on_paused_with_timeout(monkeypatch, capsys) -> None:
    class Paused(FollowService):
        async def ingestion_status(self, run_id):
            return AppResponse(
                True,
                {
                    "status": {"run_id": run_id, "status": "PAUSED"},
                    "execution_status": "running",
                    "progress": {"discovered": 3, "processed": 1},
                },
            )

    monkeypatch.setattr(cli_runner, "runtime_app_service", Paused)

    code = cli.main(["ingest", "watch", "run-42", "--refresh", "0.25", "--timeout", "1"])

    err = capsys.readouterr().err
    assert code == 3
    assert "run-42" in err and "paused" in err


def test_start_wait_timeout_exits_three(monkeypatch, capsys) -> None:
    class Running(FollowService):
        async def ingestion_status(self, run_id):
            return AppResponse(
                True,
                {
                    "status": {"run_id": run_id, "status": "RUNNING"},
                    "execution_status": "running",
                    "progress": {"discovered": 3, "processed": 1},
                },
            )

    monkeypatch.setattr(cli_runner, "runtime_app_service", Running)
    monkeypatch.setattr("harborrag_app.cli.commands.ingest._FOLLOW_INTERVAL", 0.05)

    code = cli.main(["ingest", "start", "--connector-id", "ws", "--wait", "--timeout", "1"])

    err = capsys.readouterr().err
    assert code == 3
    assert "run-42" in err and "running" in err


def test_completed_uppercase_status_still_exits_zero(monkeypatch, capsys) -> None:
    class Upper(FollowService):
        async def ingestion_result(self, run_id):
            return AppResponse(True, {"result": {"task_id": run_id, "status": "COMPLETED"}})

    monkeypatch.setattr(cli_runner, "runtime_app_service", Upper)

    assert cli.main(["ingest", "wait", "run-42", "--json"]) == 0


def test_wait_timeout_still_exits_three_when_the_status_lookup_hangs(monkeypatch, capsys) -> None:
    import asyncio
    import time

    class Hung(FollowService):
        async def ingestion_result(self, run_id):
            await asyncio.sleep(30)

        async def ingestion_status(self, run_id):
            await asyncio.sleep(30)

    monkeypatch.setattr(cli_runner, "runtime_app_service", Hung)
    monkeypatch.setattr(cli_runner, "_STATUS_LOOKUP_SECONDS", 0.2)

    started = time.monotonic()
    code = cli.main(["ingest", "wait", "run-42", "--timeout", "1"])

    assert code == 3
    assert time.monotonic() - started < 5
    assert "Run run-42 is still running" in capsys.readouterr().err


def test_ambiguous_start_failure_points_the_cli_at_ingest_status(monkeypatch, capsys) -> None:
    """The shared message is neutral; only the CLI rendering names the CLI command."""

    class Ambiguous(FollowService):
        async def start_ingestion(self, **kwargs):  # type: ignore[override]
            return AppResponse(
                False,
                {
                    "run": {"run_id": kwargs["run_id"], "status": "PENDING"},
                    "error_type": "WorkflowSubmissionError",
                },
                f"Submission failed. The run may still start; check its status with run id "
                f"{kwargs['run_id']}",
            )

    monkeypatch.setattr(cli_runner, "runtime_app_service", Ambiguous)

    argv = ["--no-color", "ingest", "start", "ws", "--run-id", "run-amb"]
    assert cli.main(argv) == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "harborrag ingest status run-amb" in err

    assert cli.main([*argv, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "harborrag ingest status" not in payload["error"]


@pytest.mark.parametrize("name", ["start", "run"])
def test_filters_json_help_scopes_key_validation_to_local_connectors(name) -> None:
    """Only the local connector validates filter keys; the help must not promise more."""

    import typer

    from harborrag_app.cli.commands import ingest

    group = typer.main.get_command(ingest.app)
    command = group.commands[name]  # type: ignore[attr-defined]
    option = next(param for param in command.params if "--filters-json" in param.opts)
    assert option.help.endswith("unknown keys are rejected for local connectors.")


class _GeneratedRunService(FollowService):
    """Echoes the run id the CLI submits, as the real service does."""

    submitted: list[dict[str, object]]

    def __init__(self) -> None:
        super().__init__()
        self.submitted = []

    async def start_ingestion(self, **kwargs):  # type: ignore[override]
        assert kwargs["wait"] is False
        self.submitted.append(kwargs)
        return AppResponse(
            True,
            {"run": {"run_id": kwargs["run_id"], "status": "PENDING"}, "workflow": {}},
        )

    async def ingestion_status(self, run_id):
        return AppResponse(
            True,
            {
                "status": {"run_id": run_id, "status": "RUNNING"},
                "execution_status": "running",
                "progress": {"discovered": 3, "processed": 1},
            },
        )


def test_start_wait_json_timeout_reports_the_generated_run_id(monkeypatch, capsys) -> None:
    """Without --run-id the CLI generates the id up front, so a timeout can still name it."""

    import asyncio

    services: list[_GeneratedRunService] = []

    class Slow(_GeneratedRunService):
        async def ingestion_result(self, run_id):
            await asyncio.sleep(30)

    def make() -> Slow:
        services.append(Slow())
        return services[-1]

    monkeypatch.setattr(cli_runner, "runtime_app_service", make)

    code = cli.main(["ingest", "start", "ws", "--wait", "--json", "--timeout", "1"])

    captured = capsys.readouterr()
    run_id = services[0].submitted[0]["run_id"]
    assert isinstance(run_id, str) and run_id.startswith("ingest-")
    assert code == 3
    assert json.loads(captured.out) == {
        "ok": True,
        "data": {"run": {"run_id": run_id, "status": "running"}, "timed_out": True},
        "error": None,
    }
    assert f"Run {run_id} is still running" in captured.err


def test_start_wait_timeout_never_bounds_the_submission(monkeypatch, capsys) -> None:
    """The deadline covers waiting for the result only, never registration or the start."""

    import asyncio

    class SlowSubmit(_GeneratedRunService):
        async def start_ingestion(self, **kwargs):  # type: ignore[override]
            await asyncio.sleep(1.3)
            return await super().start_ingestion(**kwargs)

    monkeypatch.setattr(cli_runner, "runtime_app_service", SlowSubmit)

    code = cli.main(
        ["ingest", "start", "ws", "--run-id", "r-1", "--wait", "--json", "--timeout", "1"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["data"]["result"]["status"] == "completed"


def test_start_wait_json_result_failure_keeps_the_run(monkeypatch, capsys) -> None:
    class Lost(_GeneratedRunService):
        async def ingestion_result(self, run_id):
            return AppResponse(False, {"error_type": "WorkflowNotFoundError"}, "not found")

    monkeypatch.setattr(cli_runner, "runtime_app_service", Lost)

    assert cli.main(["ingest", "start", "ws", "--run-id", "r-2", "--wait", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["data"]["run"]["run_id"] == "r-2"
    assert payload["data"]["error_type"] == "WorkflowNotFoundError"


def test_wait_json_timeout_prints_the_timed_out_envelope(monkeypatch, capsys) -> None:
    import asyncio

    class Slow(_GeneratedRunService):
        async def ingestion_result(self, run_id):
            await asyncio.sleep(30)

    monkeypatch.setattr(cli_runner, "runtime_app_service", Slow)

    code = cli.main(["ingest", "wait", "run-42", "--json", "--timeout", "1"])

    captured = capsys.readouterr()
    assert code == 3
    assert json.loads(captured.out) == {
        "ok": True,
        "data": {"run": {"run_id": "run-42", "status": "running"}, "timed_out": True},
        "error": None,
    }
    assert "Run run-42 is still running" in captured.err


@pytest.mark.parametrize("as_json", [False, True])
def test_wait_ctrl_c_leaves_the_run_running(as_json, monkeypatch, capsys) -> None:
    import asyncio

    class Interrupted(FollowService):
        async def ingestion_result(self, run_id):
            raise asyncio.CancelledError

    monkeypatch.setattr(cli_runner, "runtime_app_service", Interrupted)

    code = cli.main(["ingest", "wait", "run-42", *(["--json"] if as_json else [])])

    err = capsys.readouterr().err
    assert code == 130
    assert "Left run run-42 running; re-attach with: harborrag ingest watch run-42" in err


def test_start_wait_json_ctrl_c_names_the_generated_run(monkeypatch, capsys) -> None:
    import asyncio

    services: list[_GeneratedRunService] = []

    class Interrupted(_GeneratedRunService):
        async def ingestion_result(self, run_id):
            raise asyncio.CancelledError

    def make() -> Interrupted:
        services.append(Interrupted())
        return services[-1]

    monkeypatch.setattr(cli_runner, "runtime_app_service", make)

    code = cli.main(["ingest", "start", "ws", "--wait", "--json"])

    run_id = services[0].submitted[0]["run_id"]
    err = capsys.readouterr().err
    assert code == 130
    assert f"Left run {run_id} running; re-attach with: harborrag ingest watch {run_id}" in err


@pytest.mark.parametrize(
    ("task_status", "expected_code", "expected_status"),
    [("SUCCESS", 0, "completed"), ("FAILED", 1, "failed"), ("PARTIAL", 1, "partial")],
)
def test_watch_settles_on_the_control_plane_record_when_temporal_forgot_the_run(
    task_status, expected_code, expected_status, monkeypatch, capsys
) -> None:
    """Expired Temporal history: the terminal control-plane snapshot is the outcome.

    `ingestion_result` would only answer WorkflowNotFound, turning a COMPLETED run
    into exit 1, so it must not be called.
    """

    services: list[FollowService] = []

    class Expired(FollowService):
        async def ingestion_status(self, run_id):
            return AppResponse(
                True,
                {
                    "status": {"run_id": run_id, "status": task_status},
                    "execution_status": "not_found",
                    "source": "control_plane",
                    "progress": {"discovered": 3, "processed": 3},
                    "task": {
                        "task_id": run_id,
                        "status": task_status,
                        "progress": {"discovered": 3, "processed": 3},
                    },
                },
            )

        async def ingestion_result(self, run_id):
            self.result_calls.append(run_id)
            return AppResponse(False, {"error_type": "WorkflowNotFoundError"}, "not found")

    def make() -> Expired:
        services.append(Expired())
        return services[-1]

    monkeypatch.setattr(cli_runner, "runtime_app_service", make)

    code = cli.main(["ingest", "watch", "run-old", "--refresh", "0.25", "--events"])

    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == expected_code
    assert services[0].result_calls == []
    assert final["ok"] is True
    assert final["data"]["result"]["status"] == expected_status
    assert final["data"]["source"] == "control_plane"
