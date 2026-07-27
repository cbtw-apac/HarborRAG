from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from harborrag_adapters.models.runtime.routing_state import RoutingAdmissionError
from harborrag_adapters.models.runtime.routing_state_memory import (
    InMemoryRoutingStateStore,
)
from harborrag_adapters.models.runtime.singleflight import InMemorySingleFlight

pytestmark = [pytest.mark.performance, pytest.mark.load]


class Result(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_singleflight_coalesces_one_hundred_concurrent_requests() -> None:
    coordinator = InMemorySingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def producer() -> Result:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return Result(value=42)

    async def loader() -> Result | None:
        return None

    leader = asyncio.create_task(coordinator.aexecute("same", producer, loader))
    await started.wait()
    followers = [
        asyncio.create_task(coordinator.aexecute("same", producer, loader)) for _ in range(99)
    ]
    release.set()
    results = await asyncio.gather(leader, *followers)
    assert calls == 1
    assert sum(result.shared for result in results) == 99
    assert all(result.value.value == 42 for result in results)


def test_admission_enforces_concurrency_under_burst() -> None:
    store = InMemoryRoutingStateStore(clock=lambda: 60.0)
    leases = [
        store.acquire(
            "primary:a",
            max_parallel=10,
            rpm=20,
            tpm=1_000,
            token_cost=10,
            lease_seconds=30,
        )
        for _ in range(10)
    ]
    with pytest.raises(RoutingAdmissionError, match="concurrency"):
        store.acquire(
            "primary:a",
            max_parallel=10,
            rpm=20,
            tpm=1_000,
            token_cost=10,
            lease_seconds=30,
        )
    for lease in leases:
        store.release(lease)
    assert store.snapshot("primary:a").active_requests == 0
