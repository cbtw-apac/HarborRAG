"""Unit coverage for the working-memory tier."""

from __future__ import annotations

import time

import pytest
from harborrag_core.ports.memory import MemoryOwner

from harborrag_memory.tiers.working import InMemoryWorkingMemoryStore, WorkingMemory

_OWNER = MemoryOwner(
    tenant_id="ACME", principal_id="user-1", session_id="session-1", run_id="run-1"
)


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_scratch_defaults_to_an_empty_dict() -> None:
    memory = WorkingMemory(InMemoryWorkingMemoryStore())
    assert await memory.scratch(_OWNER) == {}


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_update_then_scratch_round_trips() -> None:
    memory = WorkingMemory(InMemoryWorkingMemoryStore())
    await memory.update(_OWNER, {"step": 3, "notes": ["a", "b"]})
    assert await memory.scratch(_OWNER) == {"step": 3, "notes": ["a", "b"]}


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_clear_removes_state() -> None:
    memory = WorkingMemory(InMemoryWorkingMemoryStore())
    await memory.update(_OWNER, {"step": 1})
    await memory.clear(_OWNER)
    assert await memory.scratch(_OWNER) == {}


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_state_expires_after_its_ttl() -> None:
    memory = WorkingMemory(InMemoryWorkingMemoryStore())
    await memory.update(_OWNER, {"step": 1}, ttl_seconds=1)
    assert await memory.scratch(_OWNER) == {"step": 1}
    time.sleep(1.05)
    assert await memory.scratch(_OWNER) == {}


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_scratch_requires_a_run_scoped_owner() -> None:
    memory = WorkingMemory(InMemoryWorkingMemoryStore())
    owner_without_run = MemoryOwner(tenant_id="ACME", principal_id="user-1")

    with pytest.raises(ValueError, match="run_id"):
        await memory.scratch(owner_without_run)


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_working_memory_for_different_runs_is_isolated() -> None:
    store = InMemoryWorkingMemoryStore()
    memory = WorkingMemory(store)
    other_run = MemoryOwner(
        tenant_id="ACME", principal_id="user-1", session_id="session-1", run_id="run-2"
    )

    await memory.update(_OWNER, {"step": 1})
    assert await memory.scratch(other_run) == {}
