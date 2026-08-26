from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_adapters.repositories.errors import HarborStorageLeaseError
from harborrag_adapters.repositories.state.redis.stores import RedisLeaseStore
from harborrag_core.schemas.state import LeaseRecord

from .fakes import CONTEXT, FakeStateBackend, FakeStateClient


@pytest.mark.asyncio
async def test_lease_acquire_returns_lease_when_available() -> None:
    client = FakeStateClient(eval_results=[9])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]

    lease = await store.acquire("resource", "owner", timedelta(seconds=30), context=CONTEXT)

    assert lease is not None
    assert lease.fencing_token == 9
    assert lease.resource == "resource"


@pytest.mark.asyncio
async def test_lease_acquire_returns_none_when_unavailable() -> None:
    client = FakeStateClient(eval_results=[0])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]

    lease = await store.acquire("resource", "owner", timedelta(seconds=30), context=CONTEXT)

    assert lease is None


@pytest.mark.asyncio
async def test_lease_acquire_rejects_non_positive_duration() -> None:
    client = FakeStateClient(eval_results=[1])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="lease_duration must be positive"):
        await store.acquire("resource", "owner", timedelta(0), context=CONTEXT)


@pytest.mark.asyncio
async def test_lease_renew_succeeds() -> None:
    client = FakeStateClient(eval_results=[1])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]
    lease = LeaseRecord(
        resource="resource",
        owner_token="owner",
        fencing_token=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    renewed = await store.renew(lease, timedelta(seconds=60), context=CONTEXT)

    assert renewed.expires_at > lease.expires_at


@pytest.mark.asyncio
async def test_lease_renew_rejects_non_positive_duration() -> None:
    client = FakeStateClient(eval_results=[1])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]
    lease = LeaseRecord(
        resource="resource",
        owner_token="owner",
        fencing_token=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="lease_duration must be positive"):
        await store.renew(lease, timedelta(0), context=CONTEXT)


@pytest.mark.asyncio
async def test_lease_renew_raises_lease_error_when_not_owned() -> None:
    client = FakeStateClient(eval_results=[0])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]
    lease = LeaseRecord(
        resource="resource",
        owner_token="owner",
        fencing_token=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    with pytest.raises(HarborStorageLeaseError):
        await store.renew(lease, timedelta(seconds=30), context=CONTEXT)


@pytest.mark.asyncio
async def test_lease_release_true_and_false() -> None:
    lease = LeaseRecord(
        resource="resource",
        owner_token="owner",
        fencing_token=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    released_true = await RedisLeaseStore(
        FakeStateBackend(FakeStateClient(eval_results=[1]))  # type: ignore[arg-type]
    ).release(lease, context=CONTEXT)
    released_false = await RedisLeaseStore(
        FakeStateBackend(FakeStateClient(eval_results=[0]))  # type: ignore[arg-type]
    ).release(lease, context=CONTEXT)

    assert released_true is True
    assert released_false is False
