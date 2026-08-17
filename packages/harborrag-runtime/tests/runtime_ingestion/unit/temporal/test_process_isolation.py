"""Tests for subprocess isolation: orchestration, cancellation, and
hung-subprocess heartbeat suspension.

Serialization/crash-surfacing behavior (pickling failures, worker-level
pre-serialization) is split out in test_process_isolation_serialization.py.
"""

from __future__ import annotations

import asyncio
import pickle
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
    monkeypatch.setattr(
        module.activity,
        "info",
        lambda: type("Info", (), {"heartbeat_timeout": None})(),
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
        [("alive", None), ("done", pickle.dumps("expected-value"))],
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
@pytest.mark.parametrize("invalid_interval", [0, -1, float("nan"), float("inf"), float("-inf")])
async def test_run_in_isolated_subprocess_rejects_invalid_interval(
    invalid_interval: float,
) -> None:
    """Non-finite and non-positive intervals are rejected before spawning."""
    with pytest.raises(ValueError, match="finite and positive"):
        await module.run_in_isolated_subprocess(_noop_sync, heartbeat_interval=invalid_interval)


@pytest.mark.asyncio
async def test_run_in_isolated_subprocess_rejects_interval_at_heartbeat_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interval must be shorter than the configured heartbeat_timeout."""
    from datetime import timedelta

    monkeypatch.setattr(
        module.activity,
        "info",
        lambda: type("Info", (), {"heartbeat_timeout": timedelta(seconds=1)})(),
    )

    with pytest.raises(ValueError, match="heartbeat_timeout"):
        await module.run_in_isolated_subprocess(_noop_sync, heartbeat_interval=1.0)


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
