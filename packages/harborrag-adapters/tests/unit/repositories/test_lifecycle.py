from __future__ import annotations

import pytest
from harborrag_adapters.repositories.lifecycle import AsyncLifecycle, RepositoryLifecycle
from harborrag_core.schemas.storage import HealthStatus, RepositoryHealth, StorageFamily


class _RecordingLifecycle(AsyncLifecycle):
    """Minimal concrete AsyncLifecycle used to exercise the base protocol."""

    def __init__(self, *, fail_connect: bool = False, fail_close: bool = False) -> None:
        self.connected = False
        self.close_calls = 0
        self._fail_connect = fail_connect
        self._fail_close = fail_close

    async def connect(self) -> None:
        if self._fail_connect:
            raise RuntimeError("connect failed")
        self.connected = True

    async def close(self) -> None:
        self.close_calls += 1
        if self._fail_close:
            raise RuntimeError("close failed")
        self.connected = False


@pytest.mark.asyncio
async def test_aenter_returns_self_and_connects() -> None:
    lifecycle = _RecordingLifecycle()
    async with lifecycle as entered:
        assert entered is lifecycle
        assert lifecycle.connected is True
    assert lifecycle.connected is False
    assert lifecycle.close_calls == 1


@pytest.mark.asyncio
async def test_aenter_closes_partial_resource_and_reraises_on_connect_failure() -> None:
    lifecycle = _RecordingLifecycle(fail_connect=True)
    with pytest.raises(RuntimeError, match="connect failed"):
        async with lifecycle:
            pass  # pragma: no cover - must never run
    assert lifecycle.close_calls == 1


@pytest.mark.asyncio
async def test_aenter_swallows_close_error_during_failure_cleanup() -> None:
    lifecycle = _RecordingLifecycle(fail_connect=True, fail_close=True)
    # The original connect failure must propagate, not the secondary close failure.
    with pytest.raises(RuntimeError, match="connect failed"):
        async with lifecycle:
            pass  # pragma: no cover - must never run
    assert lifecycle.close_calls == 1


@pytest.mark.asyncio
async def test_aexit_always_closes_even_when_body_raises() -> None:
    lifecycle = _RecordingLifecycle()
    with pytest.raises(ValueError, match="boom"):
        async with lifecycle:
            raise ValueError("boom")
    assert lifecycle.close_calls == 1
    assert lifecycle.connected is False


class _ConcreteRepositoryLifecycle(RepositoryLifecycle):
    """Minimal concrete RepositoryLifecycle used to exercise the health contract."""

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health(self) -> RepositoryHealth:
        return RepositoryHealth(
            family=StorageFamily.DATABASE,
            backend="test",
            instance_name="default",
            status=HealthStatus.HEALTHY,
        )


@pytest.mark.asyncio
async def test_repository_lifecycle_exposes_health_alongside_async_context_manager() -> None:
    lifecycle = _ConcreteRepositoryLifecycle()
    async with lifecycle:
        health = await lifecycle.health()
    assert health.status == HealthStatus.HEALTHY
