"""Shared turn leases serialize separate service instances and fail on ownership loss."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_app.workflow_control.memory.locks import (
    ConversationTurnBusyError,
    ConversationTurnLeaseLostError,
    SessionLocks,
)
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_runtime.memory import InMemoryConversationMemory

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.asyncio
async def test_separate_service_locks_share_human_ownership_and_release() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "credential-one", "session", "user")
    same_user = ConversationIdentity("tenant", "credential-two", "session", "user")
    await memory.create(identity)
    first = SessionLocks(memory)
    second = SessionLocks(memory, acquire_timeout=0.03, poll_interval=0.005)
    async with first.hold(identity):
        with pytest.raises(ConversationTurnBusyError):
            async with second.hold(same_user):
                pytest.fail("a second worker entered the active turn")
    async with second.hold(same_user):
        assert second.active(identity)
    assert not first.active(identity)
    assert not second.active(identity)


@pytest.mark.asyncio
async def test_heartbeat_keeps_a_long_turn_exclusive() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await memory.create(identity)
    first = SessionLocks(memory, lease_seconds=0.06)
    async with first.hold(identity):
        await asyncio.sleep(0.12)
        assert not await memory.acquire_turn_lease(identity, token="other", lease_seconds=1)
    assert await memory.acquire_turn_lease(identity, token="other", lease_seconds=1)


@pytest.mark.asyncio
async def test_lost_lease_cancels_inference_and_surfaces_conflict() -> None:
    class LostLeaseMemory(InMemoryConversationMemory):
        async def renew_turn_lease(
            self, identity: ConversationIdentity, *, token: str, lease_seconds: float
        ) -> bool:
            return False

    memory = LostLeaseMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await memory.create(identity)
    locks = SessionLocks(memory, lease_seconds=0.03)
    with pytest.raises(ConversationTurnLeaseLostError):
        async with locks.hold(identity):
            await asyncio.Event().wait()
    assert not locks.active(identity)
    assert await memory.acquire_turn_lease(identity, token="successor", lease_seconds=1)


@pytest.mark.asyncio
async def test_client_cancellation_releases_shared_lease() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await memory.create(identity)
    entered = asyncio.Event()
    locks = SessionLocks(memory)

    async def complete() -> None:
        async with locks.hold(identity):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(complete())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await memory.acquire_turn_lease(identity, token="successor", lease_seconds=1)


@pytest.mark.asyncio
async def test_different_sessions_can_run_in_parallel() -> None:
    memory = InMemoryConversationMemory()
    locks = SessionLocks(memory)
    identities = [
        ConversationIdentity("tenant", "principal", f"session-{index}", "user")
        for index in range(2)
    ]
    for identity in identities:
        await memory.create(identity)
    entered = [asyncio.Event(), asyncio.Event()]

    async def complete(index: int) -> None:
        async with locks.hold(identities[index]):
            entered[index].set()
            async with asyncio.timeout(1):
                await entered[1 - index].wait()

    await asyncio.gather(*(complete(index) for index in range(2)))


@pytest.mark.asyncio
async def test_same_worker_queue_has_a_bounded_wait_and_remains_usable() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await memory.create(identity)
    locks = SessionLocks(memory, acquire_timeout=0.03)

    async def another_turn() -> None:
        async with locks.hold(identity):
            pytest.fail("the waiting turn entered an already active session")

    async with locks.hold(identity):
        with pytest.raises(ConversationTurnBusyError):
            await asyncio.create_task(another_turn())
        assert locks.active(identity)
    assert not locks.active(identity)
    async with locks.hold(identity):
        assert locks.active(identity)
