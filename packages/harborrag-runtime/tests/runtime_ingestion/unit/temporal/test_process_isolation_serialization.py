"""Serialization/crash-surfacing coverage split from process isolation tests.

Covers pickling failures on both sides of the subprocess boundary: spawn-time
argument serialization, result serialization back to the parent, and the
worker's own pre-serialization of results and exceptions before they reach
the multiprocessing queue.
"""

from __future__ import annotations

import pickle

import pytest

from harborrag_runtime.temporal import process_isolation as module

from .test_process_isolation import (
    _crash_sync,
    _FakeProcess,
    _FakeQueue,
    _noop_sync,
    _patch_subprocess,
    _patch_subprocess_context,  # noqa: F401 - autouse fixture, applied via import
)


def test_encode_exception_falls_back_when_error_is_unpicklable() -> None:
    """An unpicklable exception degrades to a plain RuntimeError, not a drop."""

    class _Unpicklable(RuntimeError):
        def __reduce__(self) -> tuple[object, ...]:
            raise TypeError("cannot pickle this error")

    payload = module._encode_exception(_Unpicklable("boom"))
    error = pickle.loads(payload)
    assert isinstance(error, RuntimeError)
    assert "_Unpicklable" in str(error)


@pytest.mark.asyncio
async def test_subprocess_crash_raises_typed_application_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subprocess error message raises SubprocessCrashError (ApplicationError)."""
    monkeypatch.setattr(module.activity, "heartbeat", lambda _: None)
    _patch_subprocess(
        monkeypatch,
        [("error", pickle.dumps(RuntimeError("segfault")))],
    )

    with pytest.raises(module.SubprocessCrashError) as exc_info:
        await module.run_in_isolated_subprocess(_crash_sync, heartbeat_interval=0.01)

    assert exc_info.value.type == "SubprocessCrash"
    assert "RuntimeError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_unpicklable_result_maps_to_result_serialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result that cannot be pickled back must surface, not read as a hang."""
    monkeypatch.setattr(module.activity, "heartbeat", lambda _: None)
    failure = RuntimeError(
        "isolated subprocess result serialization failed: TypeError: cannot pickle"
    )
    _patch_subprocess(
        monkeypatch,
        [("result_error", pickle.dumps(failure))],
    )

    with pytest.raises(
        module.SubprocessResultSerializationError, match="result serialization failed"
    ) as exc_info:
        await module.run_in_isolated_subprocess(_noop_sync, heartbeat_interval=0.01)

    assert exc_info.value.type == "SubprocessCrash"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spawn_error",
    [
        AttributeError("Can't get local object 'builder'"),
        TypeError("cannot pickle 'mappingproxy' object"),
        pickle.PicklingError("unpicklable closure"),
    ],
)
async def test_start_pickling_failure_maps_to_subprocess_crash(
    monkeypatch: pytest.MonkeyPatch,
    spawn_error: Exception,
) -> None:
    """Spawn-time pickling failures should surface as SubprocessCrashError."""
    monkeypatch.setattr(module.activity, "heartbeat", lambda _: None)

    class _FakeCtx:
        def Queue(self) -> _FakeQueue:
            return _FakeQueue([])

        def Process(self, **_kwargs: object) -> _FakeProcess:
            class _StartFailsProcess(_FakeProcess):
                def start(self) -> None:
                    raise spawn_error

            return _StartFailsProcess(alive=False)

    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _: _FakeCtx())

    with pytest.raises(module.SubprocessCrashError, match="serialization failed") as exc_info:
        await module.run_in_isolated_subprocess(_noop_sync, heartbeat_interval=0.01)

    assert exc_info.value.type == "SubprocessCrash"


@pytest.mark.asyncio
async def test_non_serialization_start_failure_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated spawn failures must not be reported as serialization failures."""
    monkeypatch.setattr(module.activity, "heartbeat", lambda _: None)

    class _FakeCtx:
        def Queue(self) -> _FakeQueue:
            return _FakeQueue([])

        def Process(self, **_kwargs: object) -> _FakeProcess:
            class _StartFailsProcess(_FakeProcess):
                def start(self) -> None:
                    raise OSError("fork resource limit reached")

            return _StartFailsProcess(alive=False)

    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _: _FakeCtx())

    with pytest.raises(OSError, match="resource limit"):
        await module.run_in_isolated_subprocess(_noop_sync, heartbeat_interval=0.01)


class _RecordingQueue:
    """Queue stub that records puts without any real (de)serialization."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    def put_nowait(self, item: tuple[str, object]) -> None:
        self.messages.append(item)

    def put(self, item: tuple[str, object]) -> None:
        self.messages.append(item)


def test_subprocess_worker_pickles_a_successful_result() -> None:
    """The worker pre-serializes the result before enqueueing it."""
    queue_stub = _RecordingQueue()

    module._subprocess_worker(_noop_sync, ("value",), {}, queue_stub, heartbeat_interval=1.0)

    kind, payload = queue_stub.messages[-1]
    assert kind == "done"
    assert pickle.loads(payload) == "value"


def test_subprocess_worker_reports_unpicklable_result_as_result_error() -> None:
    """An unpicklable result surfaces as result_error instead of vanishing."""
    queue_stub = _RecordingQueue()

    def _unpicklable() -> object:
        return lambda: None  # local closures cannot be pickled

    module._subprocess_worker(_unpicklable, (), {}, queue_stub, heartbeat_interval=1.0)

    kind, payload = queue_stub.messages[-1]
    assert kind == "result_error"
    error = pickle.loads(payload)
    assert isinstance(error, RuntimeError)
    assert "result serialization failed" in str(error)


def test_subprocess_worker_pickles_a_crash_exception() -> None:
    """A raised exception is pre-serialized before it reaches the queue."""
    queue_stub = _RecordingQueue()

    module._subprocess_worker(_crash_sync, (), {}, queue_stub, heartbeat_interval=1.0)

    kind, payload = queue_stub.messages[-1]
    assert kind == "error"
    error = pickle.loads(payload)
    assert isinstance(error, RuntimeError)
    assert "parser segfault" in str(error)
