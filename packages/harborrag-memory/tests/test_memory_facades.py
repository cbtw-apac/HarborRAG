from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from harborrag_core.contracts.errors import HarborConfigurationError, HarborSecurityError
from harborrag_core.ports.conversation import ConversationIdentity, ConversationTurn
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    visible_to,
)
from harborrag_memory import (
    InMemoryWorkingMemoryStore,
    LongTermMemory,
    MemoryConfigurationError,
    MemoryManager,
    MemoryManagerConfig,
    MemoryScopeError,
    ShortTermMemory,
    WorkingMemory,
)


@dataclass
class ConversationRepositoryFake:
    values: dict[ConversationIdentity, tuple[ConversationTurn, ...]] = field(default_factory=dict)

    async def create(self, identity: ConversationIdentity) -> None:
        self.values.setdefault(identity, ())

    async def exists(self, identity: ConversationIdentity) -> bool:
        return identity in self.values

    async def recent(
        self, identity: ConversationIdentity, *, limit: int = 2
    ) -> tuple[ConversationTurn, ...]:
        return self.values.get(identity, ())[-limit:]

    async def append(self, identity: ConversationIdentity, turn: ConversationTurn) -> None:
        self.values[identity] = (*self.values.get(identity, ()), turn)

    async def clear(self, identity: ConversationIdentity) -> None:
        self.values[identity] = ()


@dataclass
class MemoryRepositoryFake:
    values: dict[str, Memory] = field(default_factory=dict)

    async def save(self, memory: Memory) -> None:
        self.values[memory.memory_id] = memory

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        memory = self.values.get(memory_id)
        if memory is None or not visible_to(memory.scope, memory.owner, caller):
            return None
        return memory

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        return tuple(
            memory
            for memory in self.values.values()
            if visible_to(memory.scope, memory.owner, query.owner)
        )[: query.limit]

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        if await self.get(caller, memory_id) is not None:
            self.values.pop(memory_id, None)


@pytest.fixture
def owner() -> MemoryOwner:
    return MemoryOwner(
        tenant_id="tenant-a",
        principal_id="user-1",
        session_id="session-1",
        run_id="run-1",
    )


def make_memory(owner: MemoryOwner, *, memory_id: str = "memory-1") -> Memory:
    return Memory(
        memory_id=memory_id,
        scope=MemoryScope.USER,
        memory_type=MemoryType.FACT,
        owner=owner,
        content="Remember this",
    )


def test_config_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="recent_turn_limit"):
        MemoryManagerConfig(recent_turn_limit=0)
    with pytest.raises(ValueError, match="working_ttl_seconds"):
        MemoryManagerConfig(working_ttl_seconds=-1)


@pytest.mark.asyncio
async def test_short_term_uses_conversation_turns_and_validates_owner(
    owner: MemoryOwner,
) -> None:
    tier = ShortTermMemory(ConversationRepositoryFake())
    turn = ConversationTurn(user_content="question", assistant_content="answer")
    await tier.append(owner, turn)
    await tier.record(owner, turn)
    assert await tier.recent(owner) == (turn, turn)
    await tier.clear(owner)
    assert await tier.recent(owner) == ()

    with pytest.raises(MemoryScopeError, match="principal_id and session_id"):
        await tier.recent(MemoryOwner(tenant_id="tenant-a"))


@pytest.mark.asyncio
async def test_in_memory_working_store_is_scoped_and_does_not_alias_state(
    owner: MemoryOwner,
) -> None:
    working = WorkingMemory(InMemoryWorkingMemoryStore(), default_ttl_seconds=30)
    state = {"nested": {"step": 1}}
    await working.update(owner, state)
    state["nested"]["step"] = 2
    fetched = await working.scratch(owner)
    assert fetched == {"nested": {"step": 1}}
    fetched["nested"]["step"] = 3
    assert await working.scratch(owner) == {"nested": {"step": 1}}

    other_run = MemoryOwner(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=owner.session_id,
        run_id="run-2",
    )
    assert await working.scratch(other_run) == {}
    await working.clear(owner)
    assert await working.scratch(owner) == {}


@pytest.mark.asyncio
async def test_working_memory_requires_run_scope_and_positive_ttl(owner: MemoryOwner) -> None:
    working = WorkingMemory(InMemoryWorkingMemoryStore())
    with pytest.raises(MemoryScopeError, match="run_id"):
        await working.scratch(
            MemoryOwner(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                session_id=owner.session_id,
            )
        )
    with pytest.raises(ValueError, match="TTL"):
        await working.update(owner, {}, ttl_seconds=0)


@pytest.mark.asyncio
async def test_long_term_rejects_forged_and_global_writes(owner: MemoryOwner) -> None:
    repository = MemoryRepositoryFake()
    tier = LongTermMemory(repository)
    memory = make_memory(owner)
    attacker = MemoryOwner(
        tenant_id="tenant-b",
        principal_id=owner.principal_id,
        session_id=owner.session_id,
        run_id=owner.run_id,
    )
    with pytest.raises(MemoryScopeError, match="not authorized"):
        await tier.save(attacker, memory)
    global_memory = Memory(
        memory_id="global-1",
        scope=MemoryScope.GLOBAL,
        memory_type=MemoryType.FACT,
        owner=owner,
        content="global",
    )
    with pytest.raises(MemoryScopeError, match="administrative"):
        await tier.save(owner, global_memory)

    await tier.remember(owner, memory)
    assert await tier.get(owner, memory.memory_id) == memory
    with pytest.raises(MemoryScopeError, match="authenticated caller"):
        await tier.search(attacker, MemoryQuery(owner=owner))
    assert await tier.search(owner, MemoryQuery(owner=owner)) == (memory,)
    await tier.forget(owner, memory.memory_id)
    assert await tier.get(owner, memory.memory_id) is None


@pytest.mark.asyncio
async def test_manager_delegates_and_builds_immutable_snapshot(owner: MemoryOwner) -> None:
    conversation = ConversationRepositoryFake()
    short = ShortTermMemory(conversation)
    working = WorkingMemory(InMemoryWorkingMemoryStore())
    repository = MemoryRepositoryFake()
    long = LongTermMemory(repository)
    manager = MemoryManager(short, working, long)
    turn = ConversationTurn(user_content="question", assistant_content="answer")
    memory = make_memory(owner)
    await manager.append(owner, turn)
    await manager.update(owner, {"nested": {"step": 1}})
    await manager.save(owner, memory)

    snapshot = await manager.snapshot(owner, query=MemoryQuery(owner=owner))
    assert snapshot.recent_turns == (turn,)
    assert snapshot.memories == (memory,)
    with pytest.raises(TypeError):
        snapshot.working_state["new"] = "value"
    with pytest.raises(TypeError):
        snapshot.working_state["nested"]["step"] = 2


@pytest.mark.asyncio
async def test_manager_rejects_missing_tiers_and_mismatched_snapshot_owner(
    owner: MemoryOwner,
) -> None:
    manager = MemoryManager()
    with pytest.raises(MemoryConfigurationError, match="short-term"):
        await manager.recent(owner)
    assert isinstance(MemoryConfigurationError("missing"), HarborConfigurationError)
    assert isinstance(MemoryScopeError("denied"), HarborSecurityError)

    other = MemoryOwner(tenant_id="tenant-b", principal_id="other")
    with pytest.raises(MemoryScopeError, match="snapshot query owner"):
        await manager.snapshot(owner, query=MemoryQuery(owner=other))
