from __future__ import annotations

import os
import time

import pytest

from harborrag_runtime.temporal.process_isolation import (
    IsolatedProcessRunner,
    ProcessLimits,
)


def _environment_values(secret_name: str) -> tuple[str | None, str | None]:
    return os.environ.get(secret_name), os.environ.get("HARBORRAG_ISOLATED_PROCESS")


def _raise_secret_bearing_error() -> None:
    raise RuntimeError("provider token=do-not-persist")


def _block() -> None:
    time.sleep(30)


def _echo(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_runner_clears_non_allowlisted_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_TEST_PROVIDER_TOKEN", "secret-value")
    runner = IsolatedProcessRunner(
        limits=ProcessLimits(wall_seconds=10),
        max_concurrency=1,
    )

    values = await runner.run(
        _environment_values,
        "HARBORRAG_TEST_PROVIDER_TOKEN",
        heartbeat=lambda: None,
        heartbeat_interval_seconds=0.05,
    )

    assert values == (None, "1")


@pytest.mark.asyncio
async def test_runner_sanitizes_child_exception_text() -> None:
    runner = IsolatedProcessRunner(
        limits=ProcessLimits(wall_seconds=10),
        max_concurrency=1,
    )

    with pytest.raises(RuntimeError) as captured:
        await runner.run(
            _raise_secret_bearing_error,
            heartbeat=lambda: None,
            heartbeat_interval_seconds=0.05,
        )

    assert str(captured.value) == "RuntimeError in isolated document worker"
    assert "do-not-persist" not in str(captured.value)


@pytest.mark.asyncio
async def test_fast_result_does_not_wait_for_the_heartbeat_interval() -> None:
    runner = IsolatedProcessRunner(
        limits=ProcessLimits(wall_seconds=10),
        max_concurrency=1,
    )
    started = time.monotonic()

    result = await runner.run(
        _echo,
        "done",
        heartbeat=lambda: None,
        heartbeat_interval_seconds=5,
    )

    assert result == "done"
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_runner_kills_worker_at_wall_timeout() -> None:
    runner = IsolatedProcessRunner(
        limits=ProcessLimits(wall_seconds=0.2),
        max_concurrency=1,
    )
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="wall timeout"):
        await runner.run(
            _block,
            heartbeat=lambda: None,
            heartbeat_interval_seconds=0.02,
        )

    assert time.monotonic() - started < 5


def test_process_limits_and_heartbeat_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="limits must be positive"):
        ProcessLimits(cpu_seconds=0)
