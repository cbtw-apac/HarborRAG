"""Tests for subprocess isolation: crash surfacing, cancellation, and
hung-subprocess heartbeat suspension."""

from __future__ import annotations

import asyncio
import queue as _stdlib_queue
import threading

import pytest

from harborrag_runtime.temporal import process_isolation as module


def _noop_sync(value: object = "result") -> object:
    return value


def _crash_sync() -> None:
    raise RuntimeError("parser segfault")


def _hang_sync() -> None:  # pragma: no cover
    import time

    time.sleep(9999)


class _FakeQueue:
    """Synchronous queue stub driven by a pre-loaded sequence of messages."""

    def __init__(self, messages: list[tuple[str, object]]) -> None:
        self._messages = list(messages)
        self._pos = 0

    def put_nowait(self, item: object) -> None:
        pass

    def put(self, item: object) -> None:
        pass

    def get(self, timeout: float = 0) -> tuple[str, object]:
        import queue

        if self._pos >= len(self._messages):
            raise queue.Empty
        msg = self._messages[self._pos]
        self._pos += 1
        return msg


class _FakeProcess:
    def __init__(self, *, alive: bool = True) -> None:
        self._alive = alive
        self.pid = 12345
        self.killed = False
        self.joined = False
        self.join_timeouts: list[float | None] = []

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def join(self, timeout: float | None = None) -> None:
        self.joined = True
        self.join_timeouts.append(timeout)


@pytest.fixture(autouse=True)
def _patch_subprocess_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace multiprocessing internals so tests never spawn real processes."""

    class _FakeCtx:
        """Fake multiprocessing context that creates fake processes and queues."""

        def Queue(self) -> _FakeQueue:
            return _FakeQueue([])

        def Process(self, **kwargs: object) -> _FakeProcess:
            return _FakeProcess()

    monkeypatch.setattr(
        module.multiprocessing,
        "get_context",
        lambda _name: _FakeCtx(),
    )
    monkeypatch.setattr(
        module.activity,
        "in_activity",
        lambda: True,
    )


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[tuple[str, object]],
    *,
    alive: bool = True,
) -> _FakeProcess:
    """Override the process/queue to use pre-defined messages."""
    fake_queue = _FakeQueue(messages)
    fake_proc = _FakeProcess(alive=alive)

    class _FakeCtx:
        def Queue(self) -> _FakeQueue:
            return fake_queue

        def Process(self, **kwargs: object) -> _FakeProcess:
            return fake_proc

    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _name: _FakeCtx())
    return fake_proc


@pytest.mark.asyncio
async def test_run_in_isolated_subprocess_returns_result_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive path: done message returns the result without error."""
    recorded: list[object] = []
    monkeypatch.setattr(module.activity, "heartbeat", recorded.append)
    proc = _patch_subprocess(
        monkeypatch,
        [("alive", None), ("done", "expected-value")],
    )

    result = await module.run_in_isolated_subprocess(
        _noop_sync,
        "expected-value",
        heartbeat_interval=0.01,
        heartbeat_detail="test-detail",
    )

    assert result == "expected-value"
    assert "test-detail" in recorded
    assert proc.joined


@pytest.mark.asyncio
async def test_subprocess_crash_raises_typed_application_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subprocess error message raises SubprocessCrashError (ApplicationError)."""
    monkeypatch.setattr(module.activity, "heartbeat", lambda _: None)
    _patch_subprocess(
        monkeypatch,
        [("error", RuntimeError("segfault"))],
    )

    with pytest.raises(module.SubprocessCrashError) as exc_info:
        await module.run_in_isolated_subprocess(_crash_sync, heartbeat_interval=0.01)

    assert exc_info.value.type == "SubprocessCrash"
    assert "RuntimeError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_start_pickling_failure_maps_to_subprocess_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn-time pickling failures should surface as SubprocessCrashError."""
    monkeypatch.setattr(module.activity, "heartbeat", lambda _: None)

    class _FakeCtx:
        def Queue(self) -> _FakeQueue:
            return _FakeQueue([])

        def Process(self, **_kwargs: object) -> _FakeProcess:
            class _StartFailsProcess(_FakeProcess):
                def start(self) -> None:
                    raise AttributeError(
                        "Can't get local object 'HarborParserRegistry.register_family.<locals>.builder'"
                    )

            return _StartFailsProcess(alive=False)

    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _: _FakeCtx())

    with pytest.raises(module.SubprocessCrashError, match="serialization failed") as exc_info:
        await module.run_in_isolated_subprocess(_noop_sync, heartbeat_interval=0.01)

    assert exc_info.value.type == "SubprocessCrash"


@pytest.mark.asyncio
async def test_cancellation_kills_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError triggers proc.kill() before re-raising inside protected path."""
    monkeypatch.setattr(module.activity, "heartbeat", lambda _: None)
    fake_proc = _FakeProcess(alive=True)

    # Signal when the polling loop calls queue.get(), ensuring we're ready.
    polling_started = threading.Event()

    class _StallQueue:
        def get(self, timeout: float = 0) -> tuple[str, object]:
            polling_started.set()
            raise _stdlib_queue.Empty

    class _FakeCtx:
        def Queue(self) -> _StallQueue:
            return _StallQueue()

        def Process(self, **_kwargs: object) -> _FakeProcess:
            return fake_proc

    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _: _FakeCtx())

    task = asyncio.create_task(
        module.run_in_isolated_subprocess(_hang_sync, heartbeat_interval=0.01)
    )

    # Yield control to allow executor to call queue.get() without blocking event loop
    for _ in range(100):
        await asyncio.sleep(0)
        if polling_started.is_set():
            break

    assert polling_started.is_set(), "polling loop must enter queue.get within 1 second"

    # Cancel inside the protected execution path ensures proc.kill() is called.
    task.cancel()

    # CancelledError is the only expected exception when cancelled in the loop.
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake_proc.killed, "subprocess must be killed on cancellation"


@pytest.mark.asyncio
async def test_hung_subprocess_stops_heartbeating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No alive signal → SubprocessCrashError; only initial heartbeat sent."""
    heartbeats: list[object] = []
    monkeypatch.setattr(module.activity, "heartbeat", heartbeats.append)
    proc = _patch_subprocess(monkeypatch, [], alive=False)

    with pytest.raises(module.SubprocessCrashError, match="hung"):
        await module.run_in_isolated_subprocess(_hang_sync, heartbeat_interval=0.01)

    assert len(heartbeats) == 1
    assert heartbeats[0] == "running"
    assert proc.joined
    assert len(proc.join_timeouts) == 1
    assert proc.join_timeouts[0] == pytest.approx(0.02, abs=0.001)
