"""MinerU runs as a child process, so the trace and document reach it through env."""

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

from harborrag_adapters.parsers.pdf.engines.mineru import engine as mineru_engine
from harborrag_adapters.parsers.pdf.engines.mineru.engine import (
    MinerUPDFEngine,
    trace_environment,
)
from harborrag_core.observability.execution_context import (
    TemporalExecutionContext,
    bound_execution,
)

_EXECUTION = TemporalExecutionContext(
    namespace="harborrag",
    workflow_id="harborrag-document:task-1:0:3",
    run_id="run-1",
    activity_id="7",
    attempt=1,
)


def test_trace_environment_is_empty_outside_a_span_and_activity() -> None:
    assert trace_environment() == {}


def test_trace_environment_carries_traceparent_and_document_execution() -> None:
    tracer = TracerProvider().get_tracer("test")

    with bound_execution(_EXECUTION), tracer.start_as_current_span("parse") as span:
        environment = trace_environment()

    trace_id = format(span.get_span_context().trace_id, "032x")
    assert environment["TRACEPARENT"].split("-")[1] == trace_id
    assert environment["HARBORRAG_DOCUMENT_EXECUTION_ID"] == "harborrag-document:task-1:0:3"


def test_mineru_subprocess_receives_trace_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Process:
        def wait(self, timeout: float | None = None) -> int:
            return 0

    def popen(command: list[str], **kwargs: Any) -> _Process:
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr(mineru_engine.subprocess, "Popen", popen)

    with bound_execution(_EXECUTION):
        MinerUPDFEngine()._run(["mineru"])

    assert captured["env"]["HARBORRAG_DOCUMENT_EXECUTION_ID"] == _EXECUTION.workflow_id


def _captured_command(monkeypatch: pytest.MonkeyPatch, *, owns_group: bool) -> list[str]:
    captured: dict[str, Any] = {}

    class _Process:
        def wait(self, timeout: float | None = None) -> int:
            return 0

    def popen(command: list[str], **kwargs: Any) -> _Process:
        captured["command"] = command
        captured["start_new_session"] = kwargs["start_new_session"]
        return _Process()

    monkeypatch.setattr(mineru_engine.subprocess, "Popen", popen)
    monkeypatch.setattr(mineru_engine, "_owns_process_group", lambda: owns_group)
    MinerUPDFEngine()._run(["mineru", "-p", "doc.pdf"])
    assert captured["start_new_session"] is owns_group
    return captured["command"]


def test_mineru_runs_under_the_traced_launcher_when_its_group_is_killable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _captured_command(monkeypatch, owns_group=True)

    assert command[1:] == [
        "-m",
        "harborrag_runtime.parser_subprocess",
        "--",
        "mineru",
        "-p",
        "doc.pdf",
    ]


def test_mineru_runs_directly_when_a_kill_would_orphan_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _captured_command(monkeypatch, owns_group=False) == ["mineru", "-p", "doc.pdf"]
