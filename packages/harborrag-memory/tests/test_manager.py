"""Unit coverage for the top-level memory facade."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_core.ports.conversation import ConversationTurn
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery, MemoryScope, MemoryType, new_memory_id
from harborrag_memory.manager import MemoryManager


@dataclass
class _FakeShortTermMemory:
    recent_turns: tuple[ConversationTurn, ...]

    def __post_init__(self) -> None:
        self.recorded: list[tuple[MemoryOwner, ConversationTurn]] = []
        self.cleared: list[MemoryOwner] = []
        self.recent_calls: list[tuple[MemoryOwner, int]] = []

    async def recent(self, owner: MemoryOwner, *, limit: int = 2) -> tuple[ConversationTurn, ...]:
        self.recent_calls.append((owner, limit))
        return self.recent_turns[-limit:]

    async def record(self, owner: MemoryOwner, turn: ConversationTurn) -> None:
        self.recorded.append((owner, turn))

    async def clear(self, owner: MemoryOwner) -> None:
        self.cleared.append(owner)


@dataclass
class _FakeWorkingMemory:
    state: dict[str, object]

    def __post_init__(self) -> None:
        self.updates: list[tuple[MemoryOwner, dict[str, object], int | None]] = []
        self.cleared: list[MemoryOwner] = []

    async def scratch(self, owner: MemoryOwner) -> dict[str, object]:
        return dict(self.state)

    async def update(
        self,
        owner: MemoryOwner,
        state: dict[str, object],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        self.updates.append((owner, dict(state), ttl_seconds))
        self.state = dict(state)

    async def clear(self, owner: MemoryOwner) -> None:
        self.cleared.append(owner)
        self.state = {}


@dataclass
class _FakeLongTermMemory:
    memories: tuple[Memory, ...]

    def __post_init__(self) -> None:
        self.saved: list[Memory] = []
        self.deleted: list[tuple[MemoryOwner, str]] = []
        self.queries: list[MemoryQuery] = []

    async def save(self, memory: Memory) -> None:
        self.saved.append(memory)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        for memory in self.saved:
            if memory.memory_id == memory_id:
                return memory
        return None

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        self.queries.append(query)
        return self.memories

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        self.deleted.append((caller, memory_id))


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_memory_manager_composes_the_three_memory_tiers() -> None:
    owner = MemoryOwner(
        tenant_id="acme",
        project_id="project-1",
        principal_id="user-1",
        session_id="session-1",
        run_id="run-1",
    )
    turn = ConversationTurn("hello", "hi there")
    memory = Memory(
        memory_id=new_memory_id(),
        scope=MemoryScope.USER,
        memory_type=MemoryType.PREFERENCE,
        owner=owner,
        content="keep answers concise",
    )
    manager = MemoryManager(
        short_term=_FakeShortTermMemory((ConversationTurn("earlier", "reply"), turn)),
        working=_FakeWorkingMemory({"step": 1}),
        long_term=_FakeLongTermMemory((memory,)),
    )

    await manager.append(owner, turn)
    await manager.update(owner, {"step": 2})
    await manager.save(memory)

    snapshot = await manager.snapshot(
        owner,
        query=MemoryQuery(owner=owner, limit=1),
        recent_limit=1,
    )

    assert snapshot.recent_turns == (turn,)
    assert snapshot.working_state == {"step": 2}
    assert snapshot.memories == (memory,)
