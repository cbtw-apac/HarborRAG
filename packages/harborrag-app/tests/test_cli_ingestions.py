"""``harborrag ingestions``: start, --wait, --inline, list, get --watch and the run actions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from app_test_fixtures import MockAppService
from cli_fakes import Call, FakeOperations, serving

from harborrag_app.api.v1.admin.connection_schemas import IngestionOut, IngestionStartRequest
from harborrag_app.api.v1.ingestion.schemas import IngestionListQuery, IngestionPage
from harborrag_app.cli import follow, invoke
from harborrag_app.cli import main as cli
from harborrag_app.cli.commands import ingestions
from harborrag_app.cli.invoke import Outcome
from harborrag_app.operations import Accepted, ConnectionRef
from harborrag_app.workflow_control import AppResponse
from harborrag_app.workflow_control.composition import service as service_module
from harborrag_app.workflow_control.control_plane.connections import ConnectionKey
from harborrag_app.workflow_control.ingestion import connection_sources
from harborrag_core.contracts.errors import HarborAlreadyRunningError, HarborCapabilityError
from harborrag_core.domain.connection import Connection
from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.execution import direct as runtime_direct

pytestmark = pytest.mark.unit

_STAMP = datetime(2026, 11, 10, tzinfo=UTC)
_REF = "default/harborrag-workspace"
_STACK_SOURCE_PATH = "/data/sources"
_LINE = re.compile(
    r"^ing_1  (\w+)  discovered=\d+ processed=\d+ skipped=\d+ failed=(\d+)$", re.MULTILINE
)
_COMPOSITION = logging.getLogger("harborrag.runtime.composition.control_plane")
_WARNING = "Control-plane schema check skipped"


def _ingestion(status: str = "queued", *, processed: int = 0, failed: int = 0) -> IngestionOut:
    return IngestionOut.model_validate(
        {
            "ingestion_id": "ing_1",
            "tenant_id": "DEFAULT",
            "project_id": "default",
            "connection_id": "harborrag-workspace",
            "mode": "incremental",
            "status": status,
            "counters": {
                "discovered": 4,
                "processed": processed,
                "skipped": 0,
                "failed": failed,
            },
            "stages": [],
            "error_category": None,
            "triggered_by": {"principal_type": "user", "principal_id": "cli:alex"},
            "settings_revision": "",
            "workflow_id": "harborrag-source:ing_1",
            "run_id": "ing_1",
            "trace_id": "",
            "links": {"temporal": "", "grafana": "", "langfuse": ""},
            "created_at": _STAMP,
            "finished_at": None,
        }
    )


def _accepted() -> Accepted[IngestionOut]:
    return Accepted(_ingestion(), "/v1/admin/ingestions/ing_1")


class _ScriptedRuns(FakeOperations):
    """Answers each ``get_ingestion`` with the next state of the run."""

    def __init__(self, states: list[IngestionOut]) -> None:
        super().__init__(results={"start_ingestion": _accepted()})
        self.states = list(states)

    async def get_ingestion(self, *args: object, **kwargs: object) -> IngestionOut:  # type: ignore[override]  # the canned-result method is generic
        self.calls.append(Call("get_ingestion", args, dict(kwargs)))
        return self.states.pop(0)


class _Sleeps:
    """Stands in for ``asyncio.sleep``: records each pause, or raises ``interrupt`` at the first."""

    def __init__(self, interrupt: BaseException | None = None) -> None:
        self.seconds: list[float] = []
        self.interrupt = interrupt
        self.on_pause: Callable[[], None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        if self.on_pause is not None:
            self.on_pause()
        if self.interrupt is not None:
            raise self.interrupt


@pytest.fixture(autouse=True)
def _hermetic(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("HARBORRAG_TENANT_ID", "HARBORRAG_OUTPUT", "HARBORRAG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HARBORRAG_LOG_LEVEL", raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> _Sleeps:
    pauses = _Sleeps()
    monkeypatch.setattr(
        ingestions, "follow_ingestion", partial(follow.follow_ingestion, sleep=pauses)
    )
    return pauses


def _serve(monkeypatch: pytest.MonkeyPatch, operations: FakeOperations) -> FakeOperations:
    monkeypatch.setattr(invoke, "local_operations", serving(operations))
    return operations


def _serving_after_a_warning(
    operations: FakeOperations,
) -> Callable[[], AbstractAsyncContextManager[FakeOperations]]:
    """``local_operations`` whose service build logs a warning, as composition can."""

    @asynccontextmanager
    async def local_operations() -> AsyncIterator[FakeOperations]:
        _COMPOSITION.warning(_WARNING)
        yield operations

    return local_operations


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _called(operations: FakeOperations) -> list[str]:
    return [call.operation_id for call in operations.calls]


def test_wait_prints_a_line_per_change_and_exits_0_when_items_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sleeps: _Sleeps
) -> None:
    fake = _serve(
        monkeypatch,
        _ScriptedRuns(
            [
                _ingestion("queued"),
                _ingestion("running", processed=2),
                _ingestion("running", processed=2),
                _ingestion("succeeded", processed=2, failed=2),
            ]
        ),
    )

    code, out, _ = _run(capsys, "ingestions", "start", _REF, "--wait", "-o", "table")

    assert code == 0
    assert [(status, failed) for status, failed in _LINE.findall(out)] == [
        ("queued", "0"),
        ("running", "0"),
        ("succeeded", "2"),
    ]
    assert sleeps.seconds == [follow.POLL_SECONDS] * 3
    start = fake.calls[0]
    assert start.operation_id == "start_ingestion"
    assert start.args[1] == IngestionStartRequest(
        project_id="default", connection_id="harborrag-workspace"
    )


@pytest.mark.parametrize("ending", ["cancelled", "failed"])
def test_wait_exits_8_when_the_run_does_not_succeed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sleeps: _Sleeps,
    ending: str,
) -> None:
    _serve(monkeypatch, _ScriptedRuns([_ingestion("running"), _ingestion(ending)]))

    code, out, _ = _run(capsys, "ingestions", "start", _REF, "--wait", "-o", "json")

    assert code == 8
    assert json.loads(out)["status"] == ending


def test_wait_in_json_prints_only_the_final_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sleeps: _Sleeps
) -> None:
    _serve(monkeypatch, _ScriptedRuns([_ingestion("running"), _ingestion("succeeded")]))

    code, out, _ = _run(capsys, "ingestions", "start", _REF, "--wait", "-o", "json")

    assert code == 0
    assert json.loads(out) == json.loads(_ingestion("succeeded").model_dump_json())


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_ctrl_c_stops_following_leaves_the_run_going_and_says_how_to_reattach(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sleeps: _Sleeps,
    interrupt: BaseException,
) -> None:
    sleeps.interrupt = interrupt
    fake = _serve(monkeypatch, _ScriptedRuns([_ingestion("running")]))

    code, _, err = _run(capsys, "ingestions", "start", _REF, "--wait", "-o", "json")

    assert code == 130
    assert (
        "Stopped following; the run keeps going. Re-attach with: "
        "harborrag ingestions get ing_1 --watch"
    ) in err
    assert "cancel_ingestion" not in _called(fake)


def test_item_refs_reach_the_operation_and_its_501_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _serve(
        monkeypatch,
        FakeOperations(
            errors={
                "start_ingestion": HarborCapabilityError(
                    "syncing selected items is not supported yet"
                )
            }
        ),
    )

    code, _, err = _run(capsys, "ingestions", "start", _REF, "--item-refs", "DEMO-1", "-o", "table")

    assert code == 2
    assert err.startswith("error 501 not_supported: ")
    assert fake.calls[0].args[1].item_refs == ["DEMO-1"]


def test_a_second_start_is_already_running_and_exits_4(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(
        monkeypatch,
        FakeOperations(
            errors={
                "start_ingestion": HarborAlreadyRunningError(
                    "an ingestion is running", details={"ingestion_id": "ing_1"}
                )
            }
        ),
    )

    code, out, err = _run(capsys, "ingestions", "start", _REF, "-o", "table")

    assert (code, out) == (4, "")
    assert err.startswith("error 409 already_running: an ingestion is running")


def test_start_without_wait_prints_the_started_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _serve(monkeypatch, FakeOperations(results={"start_ingestion": _accepted()}))

    code, out, _ = _run(
        capsys,
        "ingestions",
        "start",
        _REF,
        "--mode",
        "force",
        "--updated-after",
        "2026-11-01T00:00:00Z",
        "-o",
        "json",
    )

    assert code == 0
    assert json.loads(out)["status"] == "queued"
    body = fake.calls[0].args[1]
    assert (body.mode, body.updated_after) == ("force", datetime(2026, 11, 1, tzinfo=UTC))
    assert _called(fake) == ["start_ingestion"]


@pytest.mark.parametrize(
    "argv",
    [
        ["harborrag-workspace"],
        [_REF, "--updated-after", "2026-11-01T00:00:00"],
        [_REF, "--mode", "full"],
        [_REF, "--inline", "--wait"],
        [_REF, "--inline", "--item-refs", "DEMO-1"],
    ],
)
def test_bad_input_is_a_usage_error_before_any_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    fake = _serve(monkeypatch, FakeOperations())

    code, _, err = _run(capsys, "ingestions", "start", *argv)

    assert code == 2
    assert err
    assert fake.calls == []


def test_a_bare_connection_key_names_the_ref_form(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, FakeOperations())

    code, _, err = _run(capsys, "ingestions", "start", "harborrag-workspace")

    assert code == 2
    assert "PROJECT_ID/CONNECTION_ID" in err
    assert "harborrag connections list" in err


def test_inline_runs_in_process_and_never_starts_a_durable_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _serve(monkeypatch, FakeOperations())
    service = object()
    fake.app_service = service  # type: ignore[attr-defined]  # LocalOperations exposes it
    calls: list[tuple[Any, ...]] = []

    async def run_inline(*args: object, **kwargs: object) -> Outcome:
        calls.append((*args, kwargs))
        return Outcome({"task_id": "ingest-1", "status": "completed"}, 0)

    monkeypatch.setattr(ingestions, "run_inline", run_inline)

    code, out, _ = _run(
        capsys, "ingestions", "start", _REF, "--inline", "--mode", "force", "-o", "json"
    )

    assert code == 0
    assert json.loads(out) == {"task_id": "ingest-1", "status": "completed"}
    [(given, _, ref, options)] = calls
    assert (given, ref) == (service, ConnectionRef("default", "harborrag-workspace"))
    assert options == {"mode": "force", "updated_after": None}
    assert "start_ingestion" not in _called(fake)


def _local_file(source_path: str) -> Connection:
    return Connection(
        id="conn_1",
        tenant_id="DEFAULT",
        project_id="default",
        connection_id="harborrag-workspace",
        source_key="harborrag-workspace",
        source_type="local_file",
        name="HarborRAG workspace",
        config={"source_path": source_path},
    )


class _StoredConnections:
    """The one stored connection ``--inline`` resolves."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.keys: list[ConnectionKey] = []

    async def stored(self, key: ConnectionKey) -> Connection:
        self.keys.append(key)
        return self.connection


def _inline_service(
    connection: Connection, *, final_status: str = "completed", response: AppResponse | None = None
) -> type[MockAppService]:
    class Service(MockAppService):
        def __init__(self) -> None:
            super().__init__()
            self.direct_final_status = final_status
            self.connection_service = _StoredConnections(connection)

        async def run_ingestion(self, **kwargs: Any) -> AppResponse:
            if response is not None:
                self.direct_runs.append(kwargs)
                return response
            return await super().run_ingestion(**kwargs)

    return Service


def _built(monkeypatch: pytest.MonkeyPatch, factory: type[MockAppService]) -> list[MockAppService]:
    built: list[MockAppService] = []

    def build() -> MockAppService:
        built.append(factory())
        return built[-1]

    monkeypatch.setattr(invoke, "runtime_app_service", build)
    return built


def test_inline_refuses_a_source_path_this_machine_does_not_have(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    imports: list[object] = []

    async def record_import(*args: object, **kwargs: object) -> None:
        imports.append(args)

    for module in (runtime_direct, service_module, connection_sources):
        monkeypatch.setattr(module, "import_connectors_once", record_import)
    built = _built(monkeypatch, _inline_service(_local_file(_STACK_SOURCE_PATH)))

    code, out, err = _run(capsys, "ingestions", "start", _REF, "--inline", "--yes", "-o", "json")

    assert (code, out) == (2, "")
    error = json.loads(err)["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [
        {
            "field": "config.source_path",
            "issue": f"{_STACK_SOURCE_PATH} is not a directory on this machine; "
            "--inline reads files from here",
        }
    ]
    assert built[0].direct_runs == []
    assert imports == []


@pytest.mark.parametrize(
    ("final_status", "exit_code"),
    [
        (IngestionTaskState.COMPLETED.value, 0),
        (IngestionTaskState.PARTIAL.value, 0),
        (IngestionTaskState.FAILED.value, 8),
        (IngestionTaskState.CANCELLED.value, 8),
    ],
)
def test_inline_runs_the_stored_connection_by_its_source_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    final_status: str,
    exit_code: int,
) -> None:
    built = _built(
        monkeypatch, _inline_service(_local_file(str(tmp_path)), final_status=final_status)
    )

    code, out, _ = _run(
        capsys,
        "ingestions",
        "start",
        _REF,
        "--inline",
        "--mode",
        "force",
        "--updated-after",
        "2026-11-01T00:00:00+00:00",
        "--yes",
        "-o",
        "json",
    )

    assert code == exit_code
    assert json.loads(out)["status"] == final_status
    [run] = built[0].direct_runs
    assert (run["tenant_id"], run["connector_name"]) == ("DEFAULT", "harborrag-workspace")
    assert (run["force_reprocess"], run["updated_after"]) == (True, "2026-11-01T00:00:00+00:00")
    assert built[0].connection_service.keys == [  # type: ignore[attr-defined]
        ConnectionKey("DEFAULT", "default", "harborrag-workspace")
    ]


def test_an_inline_run_that_fails_to_run_is_one_error_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    failure = AppResponse(False, {"error_type": "RuntimeConnectionError"}, "qdrant unreachable")
    _built(monkeypatch, _inline_service(_local_file(str(tmp_path)), response=failure))

    code, out, err = _run(capsys, "ingestions", "start", _REF, "--inline", "--yes", "-o", "json")

    assert (code, out) == (1, "")
    error = json.loads(err)["error"]
    assert error["code"] == "internal_error"
    assert "qdrant unreachable" in error["message"]


def test_following_shows_held_warnings_at_once_and_keeps_them_on_exit_8(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sleeps: _Sleeps
) -> None:
    operations = _ScriptedRuns([_ingestion("running"), _ingestion("cancelled")])
    monkeypatch.setattr(invoke, "local_operations", _serving_after_a_warning(operations))
    shown_while_following: list[str] = []
    sleeps.on_pause = lambda: shown_while_following.append(capsys.readouterr().err)

    code, _, err = _run(capsys, "ingestions", "start", _REF, "--wait", "-o", "json")

    assert code == 8
    assert _WARNING in shown_while_following[0]
    assert _WARNING not in err


def test_a_held_warning_is_printed_when_the_run_exits_8_after_no_pause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sleeps: _Sleeps
) -> None:
    operations = _ScriptedRuns([_ingestion("failed")])
    monkeypatch.setattr(invoke, "local_operations", _serving_after_a_warning(operations))

    code, _, err = _run(capsys, "ingestions", "start", _REF, "--wait", "-o", "json")

    assert code == 8
    assert _WARNING in err


def test_a_failed_start_prints_only_its_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sleeps: _Sleeps
) -> None:
    operations = FakeOperations(
        errors={"start_ingestion": HarborAlreadyRunningError("an ingestion is running")}
    )
    monkeypatch.setattr(invoke, "local_operations", _serving_after_a_warning(operations))

    code, _, err = _run(capsys, "ingestions", "start", _REF, "--wait", "-o", "json")

    assert code == 4
    assert json.loads(err)["error"]["code"] == "already_running"


def test_get_watch_follows_without_starting_and_exits_8_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sleeps: _Sleeps
) -> None:
    fake = _serve(monkeypatch, _ScriptedRuns([_ingestion("running"), _ingestion("failed")]))

    code, out, _ = _run(capsys, "ingestions", "get", "ing_1", "--watch", "-o", "table")

    assert code == 8
    assert [status for status, _ in _LINE.findall(out)] == ["running", "failed"]
    assert _called(fake) == ["get_ingestion", "get_ingestion"]


def test_get_prints_the_run_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _serve(monkeypatch, FakeOperations(results={"get_ingestion": _ingestion("running")}))

    code, out, _ = _run(capsys, "ingestions", "get", "ing_1", "-o", "json")

    assert (code, json.loads(out)["status"]) == (0, "running")
    assert fake.calls[0].args[1] == "ing_1"


@pytest.mark.parametrize(
    ("command", "operation_id"),
    [
        ("pause", "pause_ingestion"),
        ("resume", "resume_ingestion"),
        ("cancel", "cancel_ingestion"),
        ("retry-failures", "retry_ingestion_failures"),
    ],
)
def test_run_actions_call_their_operation_with_the_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    operation_id: str,
) -> None:
    fake = _serve(monkeypatch, FakeOperations(results={operation_id: _ingestion("paused")}))

    code, out, _ = _run(capsys, "ingestions", command, "ing_1", "-o", "json")

    assert (code, json.loads(out)["ingestion_id"]) == (0, "ing_1")
    [call] = fake.calls
    assert (call.operation_id, call.args[1]) == (operation_id, "ing_1")
    if operation_id == "retry_ingestion_failures":
        assert call.args[2] is None


class _PagedRuns(FakeOperations):
    """Answers each ``list_ingestions`` with the next page."""

    def __init__(self, pages: list[IngestionPage]) -> None:
        super().__init__()
        self.pages = list(pages)

    async def list_ingestions(self, *args: object, **kwargs: object) -> IngestionPage:  # type: ignore[override]  # the canned-result method is generic
        self.calls.append(Call("list_ingestions", args, dict(kwargs)))
        return self.pages.pop(0)


def test_list_all_sends_the_filters_and_follows_the_cursor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = _ingestion("failed").model_copy(update={"ingestion_id": "ing_2"})
    fake = _serve(
        monkeypatch,
        _PagedRuns(
            [
                IngestionPage(items=[_ingestion("running")], next_cursor="c1"),
                IngestionPage(items=[other], next_cursor=None),
            ]
        ),
    )

    code, out, _ = _run(
        capsys,
        "ingestions",
        "list",
        "--connection-ref",
        _REF,
        "--status",
        "failed",
        "--all",
        "-o",
        "json",
    )

    body = json.loads(out)
    assert code == 0
    assert [item["ingestion_id"] for item in body["items"]] == ["ing_1", "ing_2"]
    queries = [call.args[1] for call in fake.calls]
    assert all(isinstance(query, IngestionListQuery) for query in queries)
    assert [query.cursor for query in queries] == [None, "c1"]
    assert (queries[0].connection_ref, queries[0].status) == (_REF, "failed")
