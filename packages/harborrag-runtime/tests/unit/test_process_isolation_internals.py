"""Direct tests for the isolated-runner internals (S1 remediation).

`_process_main` and `_apply_resource_limits` only ever execute inside a spawned
child, so the parent-side suite in ``test_process_isolation.py`` cannot observe
them. This module calls them in-process with the genuinely dangerous syscalls
(`setsid`, `setrlimit`) stubbed, so the sanitisation and clamping logic is
exercised without detaching or throttling the test runner itself.
"""

from __future__ import annotations

import os
import resource
from typing import Any

import pytest

from harborrag_runtime.temporal.process_codec import (
    ProcessResultKind,
    decode_process_response,
)
from harborrag_runtime.temporal.process_isolation import (
    ProcessLimits,
    _allowed_environment,
    _apply_resource_limits,
    _process_main,
)


class FakeConnection:
    """Stand-in for the child end of the multiprocessing pipe."""

    def __init__(self, *, send_error: Exception | None = None) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._send_error = send_error

    def send_bytes(self, payload: bytes) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def isolated_environ() -> Any:
    """Restore the real environment after `_process_main` clears it."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _echo(value: str) -> str:
    return value


def _boom() -> None:
    raise RuntimeError("token=do-not-leak")


def _load(payload: bytes) -> tuple[str, Any]:
    return decode_process_response(payload, ProcessResultKind.JSON)


def _dump(call: Any, args: tuple[Any, ...]) -> bytes:
    import cloudpickle

    return cloudpickle.dumps((call, args))


# --------------------------------------------------------------------------
# Child entry point
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("isolated_environ")
def test_process_main_sends_result_and_narrows_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setattr(
        "harborrag_runtime.temporal.process_isolation._apply_resource_limits",
        lambda limits: None,
    )
    connection = FakeConnection()

    _process_main(connection, _dump(_echo, ("hello",)), ProcessLimits(), {"PATH": "/usr/bin"})

    assert connection.closed is True
    kind, value = _load(connection.sent[0])
    assert (kind, value) == ("result", "hello")
    assert os.environ["PATH"] == "/usr/bin"
    assert os.environ["HARBORRAG_ISOLATED_PROCESS"] == "1"


@pytest.mark.usefixtures("isolated_environ")
def test_process_main_sends_only_the_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error envelope must not carry the child's exception text (S6)."""
    monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setattr(
        "harborrag_runtime.temporal.process_isolation._apply_resource_limits",
        lambda limits: None,
    )
    connection = FakeConnection()

    _process_main(connection, _dump(_boom, ()), ProcessLimits(), {})

    kind, value = _load(connection.sent[0])
    assert kind == "error"
    assert value == {"module": "builtins", "type": "RuntimeError"}
    assert "do-not-leak" not in repr(value)


@pytest.mark.usefixtures("isolated_environ")
def test_process_main_rejects_an_oversized_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setattr(
        "harborrag_runtime.temporal.process_isolation._apply_resource_limits",
        lambda limits: None,
    )
    connection = FakeConnection()
    limits = ProcessLimits(max_result_bytes=1)

    _process_main(connection, _dump(_echo, ("x" * 1024,)), limits, {})

    kind, value = _load(connection.sent[0])
    assert kind == "error"
    assert value["type"] == "MemoryError"


@pytest.mark.usefixtures("isolated_environ")
def test_process_main_survives_a_broken_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead parent must not turn into an unhandled child traceback."""
    monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setattr(
        "harborrag_runtime.temporal.process_isolation._apply_resource_limits",
        lambda limits: None,
    )
    connection = FakeConnection(send_error=BrokenPipeError("parent is gone"))

    _process_main(connection, _dump(_boom, ()), ProcessLimits(), {})

    assert connection.sent == []
    assert connection.closed is True


# --------------------------------------------------------------------------
# Resource limits
# --------------------------------------------------------------------------


def test_apply_resource_limits_clamps_to_the_existing_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(resource, "getrlimit", lambda name: (0, 128))
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda name, values: recorded.__setitem__(name, values),
    )

    _apply_resource_limits(ProcessLimits(cpu_seconds=600, open_files=256))

    # Requested 256 open files but the hard limit is 128, so both are clamped.
    assert recorded[resource.RLIMIT_NOFILE] == (128, 128)
    assert recorded[resource.RLIMIT_CPU] == (128, 128)


def test_apply_resource_limits_uses_the_request_when_hard_is_unlimited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(
        resource, "getrlimit", lambda name: (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    )
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda name, values: recorded.__setitem__(name, values),
    )

    _apply_resource_limits(ProcessLimits(cpu_seconds=42, open_files=99))

    assert recorded[resource.RLIMIT_CPU] == (42, 42)
    assert recorded[resource.RLIMIT_NOFILE] == (99, 99)


def test_apply_resource_limits_is_a_noop_off_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    called: list[object] = []
    monkeypatch.setattr(resource, "setrlimit", lambda *a: called.append(a))

    _apply_resource_limits(ProcessLimits())

    assert called == []


def test_allowed_environment_passes_only_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HF_HOME", "/models")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")

    allowed = _allowed_environment()

    assert allowed["PATH"] == "/usr/bin"
    assert allowed["HF_HOME"] == "/models"
    assert "AWS_SECRET_ACCESS_KEY" not in allowed
