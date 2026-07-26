from __future__ import annotations

from datetime import timedelta

import pytest

from harborrag_adapters.repositories.cache.memory.backend import MemoryCacheBackend
from harborrag_adapters.repositories.errors import HarborStorageLeaseError

from .conftest import make_context


@pytest.mark.asyncio
async def test_lock_acquire_renew_release_with_fencing() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        handle = await backend.locks.acquire(
            "resource",
            lease_duration=timedelta(seconds=30),
            wait_timeout=None,
            context=context,
        )
        assert handle is not None
        assert handle.fencing_token == 1

        # A second acquire attempt must fail while the lease is held.
        blocked = await backend.locks.acquire(
            "resource",
            lease_duration=timedelta(seconds=30),
            wait_timeout=None,
            context=context,
        )
        assert blocked is None

        renewed = await backend.locks.renew(
            handle, lease_duration=timedelta(seconds=60), context=context
        )
        assert renewed.expires_at > handle.expires_at

        assert await backend.locks.release(renewed, context=context) is True

        reacquired = await backend.locks.acquire(
            "resource",
            lease_duration=timedelta(seconds=30),
            wait_timeout=None,
            context=context,
        )
        assert reacquired is not None
        assert reacquired.fencing_token == 2


@pytest.mark.asyncio
async def test_lock_acquire_rejects_negative_wait_timeout() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        with pytest.raises(ValueError, match="wait_timeout"):
            await backend.locks.acquire(
                "resource",
                lease_duration=timedelta(seconds=30),
                wait_timeout=timedelta(seconds=-1),
                context=context,
            )


@pytest.mark.asyncio
async def test_lock_acquire_rejects_non_positive_lease_duration() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        with pytest.raises(ValueError, match="must be positive"):
            await backend.locks.acquire(
                "resource",
                lease_duration=timedelta(0),
                wait_timeout=None,
                context=context,
            )


@pytest.mark.asyncio
async def test_lock_acquire_waits_for_expiry_then_succeeds() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        first = await backend.locks.acquire(
            "contended",
            lease_duration=timedelta(milliseconds=20),
            wait_timeout=None,
            context=context,
        )
        assert first is not None

        second = await backend.locks.acquire(
            "contended",
            lease_duration=timedelta(seconds=30),
            wait_timeout=timedelta(seconds=1),
            context=context,
        )
        assert second is not None
        assert second.fencing_token == 2


@pytest.mark.asyncio
async def test_lock_renew_rejects_wrong_owner_or_expired_lease() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        handle = await backend.locks.acquire(
            "resource",
            lease_duration=timedelta(seconds=30),
            wait_timeout=None,
            context=context,
        )
        assert handle is not None
        impostor = handle.model_copy(update={"owner_token": "not-the-real-owner"})

        with pytest.raises(HarborStorageLeaseError):
            await backend.locks.renew(
                impostor, lease_duration=timedelta(seconds=30), context=context
            )


@pytest.mark.asyncio
async def test_lock_release_returns_false_for_wrong_owner() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        handle = await backend.locks.acquire(
            "resource",
            lease_duration=timedelta(seconds=30),
            wait_timeout=None,
            context=context,
        )
        assert handle is not None
        impostor = handle.model_copy(update={"owner_token": "not-the-real-owner"})

        assert await backend.locks.release(impostor, context=context) is False
