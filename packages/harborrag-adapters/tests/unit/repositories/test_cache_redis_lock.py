from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from harborrag_adapters.repositories.cache.redis import lock as lock_module
from harborrag_adapters.repositories.cache.redis.lock import RedisLockManager
from harborrag_adapters.repositories.errors import HarborStorageLeaseError
from harborrag_adapters.repositories.telemetry import RepositoryTelemetry
from harborrag_core.schemas.cache import LockHandle
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext

CONTEXT = StorageOperationContext(tenant_id="tenant-a")


class FakeLockClient:
    def __init__(self, eval_results: list[Any]) -> None:
        self._results = list(eval_results)
        self.calls: list[tuple[str, int, tuple[Any, ...]]] = []

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        self.calls.append((script, numkeys, args))
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class FakeBackend:
    def __init__(self, client: FakeLockClient) -> None:
        self.client = client
        self._telemetry = RepositoryTelemetry(None, family=StorageFamily.CACHE, backend="redis")

    @staticmethod
    def lock_key(context: StorageOperationContext, key: str) -> str:
        return f"{context.tenant_id}:lock:{key}"

    @staticmethod
    def fencing_key(context: StorageOperationContext, key: str) -> str:
        return f"{context.tenant_id}:fence:{key}"

    @staticmethod
    def error_context(operation: str, resource: str) -> dict[str, str]:
        return {"operation": operation, "resource": resource}


def make_manager(eval_results: list[Any]) -> tuple[RedisLockManager, FakeLockClient]:
    client = FakeLockClient(eval_results)
    manager = RedisLockManager(FakeBackend(client))  # type: ignore[arg-type]
    return manager, client


def make_handle(**overrides: Any) -> LockHandle:
    defaults: dict[str, Any] = {
        "key": "tenant-a:lock:resource",
        "owner_token": "owner",
        "fencing_token": 1,
        "expires_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return LockHandle(**defaults)


@pytest.mark.asyncio
async def test_acquire_returns_handle_when_lock_is_free() -> None:
    manager, client = make_manager([7])

    handle = await manager.acquire(
        "resource",
        lease_duration=timedelta(seconds=30),
        wait_timeout=None,
        context=CONTEXT,
    )

    assert handle is not None
    assert handle.fencing_token == 7
    assert handle.key == "tenant-a:lock:resource"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_acquire_returns_none_immediately_without_wait_timeout() -> None:
    manager, client = make_manager([0])

    handle = await manager.acquire(
        "resource",
        lease_duration=timedelta(seconds=30),
        wait_timeout=None,
        context=CONTEXT,
    )

    assert handle is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_acquire_retries_until_wait_timeout_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(lock_module.asyncio, "sleep", fast_sleep)
    manager, client = make_manager([0])

    handle = await manager.acquire(
        "resource",
        lease_duration=timedelta(seconds=30),
        wait_timeout=timedelta(seconds=0.05),
        context=CONTEXT,
    )

    assert handle is None
    assert len(client.calls) >= 1


@pytest.mark.asyncio
async def test_acquire_succeeds_after_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = 0

    async def fast_sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1

    monkeypatch.setattr(lock_module.asyncio, "sleep", fast_sleep)
    manager, client = make_manager([0, 0, 5])

    handle = await manager.acquire(
        "resource",
        lease_duration=timedelta(seconds=30),
        wait_timeout=timedelta(seconds=5),
        context=CONTEXT,
    )

    assert handle is not None
    assert handle.fencing_token == 5
    assert sleeps >= 1


@pytest.mark.asyncio
async def test_acquire_rejects_non_positive_lease_duration() -> None:
    manager, _client = make_manager([1])

    with pytest.raises(ValueError, match="lease_duration must be positive"):
        await manager.acquire(
            "resource",
            lease_duration=timedelta(0),
            wait_timeout=None,
            context=CONTEXT,
        )


@pytest.mark.asyncio
async def test_acquire_rejects_negative_wait_timeout() -> None:
    manager, _client = make_manager([1])

    with pytest.raises(ValueError, match="wait_timeout must not be negative"):
        await manager.acquire(
            "resource",
            lease_duration=timedelta(seconds=30),
            wait_timeout=timedelta(seconds=-1),
            context=CONTEXT,
        )


@pytest.mark.asyncio
async def test_renew_returns_updated_handle_on_success() -> None:
    manager, client = make_manager([1])
    handle = make_handle()

    renewed = await manager.renew(
        handle,
        lease_duration=timedelta(seconds=60),
        context=CONTEXT,
    )

    assert renewed.owner_token == handle.owner_token
    assert renewed.expires_at > handle.expires_at
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_renew_rejects_non_positive_lease_duration() -> None:
    manager, _client = make_manager([1])
    handle = make_handle()

    with pytest.raises(ValueError, match="lease_duration must be positive"):
        await manager.renew(handle, lease_duration=timedelta(0), context=CONTEXT)


@pytest.mark.asyncio
async def test_renew_raises_lease_error_when_not_owned() -> None:
    manager, _client = make_manager([0])
    handle = make_handle()

    with pytest.raises(HarborStorageLeaseError):
        await manager.renew(handle, lease_duration=timedelta(seconds=30), context=CONTEXT)


@pytest.mark.asyncio
async def test_release_returns_true_when_lock_was_owned() -> None:
    manager, client = make_manager([1])
    handle = make_handle()

    released = await manager.release(handle, context=CONTEXT)

    assert released is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_release_returns_false_when_not_owned() -> None:
    manager, _client = make_manager([0])
    handle = make_handle()

    released = await manager.release(handle, context=CONTEXT)

    assert released is False
