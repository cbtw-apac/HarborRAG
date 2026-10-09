"""Cross-store erasure retains enough durable state to recover partial failures."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app_test_memory import FakeMemoryIndex, FakeMemoryStore, memory

from harborrag_app.workflow_control.control_plane.effect_recovery import (
    recover_pending_control_plane_effects,
)
from harborrag_app.workflow_control.memory import administration as administration_module
from harborrag_app.workflow_control.memory.administration import (
    MemoryAdministrationService,
    PurgeTotals,
)
from harborrag_app.workflow_control.memory.erasure import ErasedMemory, MemoryErasureJournal
from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import MemoryScope
from harborrag_core.testing.control_plane_fakes import FakePendingEffectRepository
from harborrag_runtime.memory import InMemoryConversationMemory


@pytest.fixture
def stores():
    return SimpleNamespace(
        memories=FakeMemoryStore(),
        index=FakeMemoryIndex(),
        conversations=InMemoryConversationMemory(),
        pending_effects=FakePendingEffectRepository(),
    )


def administration(stores, *, journal=True):
    return MemoryAdministrationService(
        conversations=stores.conversations,
        memories=stores.memories,
        index=stores.index,
        pending_effects=stores.pending_effects if journal else None,
    )


async def recover(stores):
    # Reconstruct the service: recovery cannot depend on request-local memory.
    return await recover_pending_control_plane_effects(
        stores, erasure_handler=administration(stores).recover_erasure
    )


@pytest.mark.asyncio
async def test_vector_failure_recovers_after_canonical_row_is_gone(stores):
    row = memory("memory-1")
    await stores.memories.save(row)
    stores.index.failure = RuntimeError("index unavailable")
    await administration(stores).delete_memory(row.owner, row.memory_id)
    assert not stores.memories.rows
    assert len(stores.pending_effects.effects) == 1
    assert await recover(stores) == 0
    stores.index.failure = None
    assert await recover(stores) == 1
    assert stores.index.deleted == [row.memory_id]
    assert not stores.pending_effects.effects


@pytest.mark.asyncio
async def test_session_purge_failure_recovers_without_a_client_retry(stores, monkeypatch):
    row = memory("memory-1", scope=MemoryScope.SESSION, session_id="session-1")
    identity = ConversationIdentity(
        row.owner.tenant_id, row.owner.principal_id, row.owner.session_id, row.owner.user_id
    )
    await stores.conversations.create(identity, kind="chat")
    await stores.memories.save(row)
    search = stores.memories.search
    monkeypatch.setattr(stores.memories, "search", AsyncMock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        await administration(stores).erase_session(row.owner, actor="test")
    assert await stores.conversations.exists(identity)
    assert len(stores.pending_effects.effects) == 1
    monkeypatch.setattr(stores.memories, "search", search)
    assert await recover(stores) == 1
    assert not await stores.conversations.exists(identity)
    assert not stores.memories.rows
    assert stores.index.deleted == [row.memory_id]


@pytest.mark.asyncio
async def test_enqueue_failure_happens_before_deleting_anything(stores, monkeypatch):
    row = memory("memory-1")
    await stores.memories.save(row)
    monkeypatch.setattr(
        type(stores.pending_effects), "enqueue", AsyncMock(side_effect=RuntimeError("queue down"))
    )
    with pytest.raises(RuntimeError, match="queue down"):
        await administration(stores).delete_memory(row.owner, row.memory_id)
    assert row.memory_id in stores.memories.rows
    assert not stores.index.deleted


@pytest.mark.asyncio
async def test_canonical_delete_failure_keeps_intent(stores, monkeypatch):
    row = memory("memory-1")
    await stores.memories.save(row)
    delete = stores.memories.delete
    monkeypatch.setattr(stores.memories, "delete", AsyncMock(side_effect=RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="db down"):
        await administration(stores).delete_memory(row.owner, row.memory_id)
    assert len(stores.pending_effects.effects) == 1
    monkeypatch.setattr(stores.memories, "delete", delete)
    assert await recover(stores) == 1
    assert not stores.memories.rows
    assert stores.index.deleted == [row.memory_id]


@pytest.mark.asyncio
async def test_without_journal_index_failure_preserves_canonical_retry_handle(stores):
    row = memory("memory-1")
    await stores.memories.save(row)
    stores.index.failure = RuntimeError("index down")
    with pytest.raises(RuntimeError, match="index down"):
        await administration(stores, journal=False).delete_memory(row.owner, row.memory_id)
    assert row.memory_id in stores.memories.rows
    stores.index.failure = None
    await administration(stores, journal=False).delete_memory(row.owner, row.memory_id)
    assert not stores.memories.rows


@pytest.mark.asyncio
async def test_recovery_does_not_forget_required_index_after_configuration_change(stores):
    row = memory("memory-1")
    await stores.memories.save(row)
    stores.index.failure = RuntimeError("index down")
    await administration(stores).delete_memory(row.owner, row.memory_id)
    stores.index = None
    assert await recover(stores) == 0
    assert len(stores.pending_effects.effects) == 1


@pytest.mark.asyncio
async def test_session_recovery_remains_authorized_when_session_was_already_deleted(stores):
    from dataclasses import asdict

    from harborrag_core.domain.pending_effect import PendingControlPlaneEffect

    row = memory("memory-1", scope=MemoryScope.SESSION, session_id="session-1")
    await stores.memories.save(row)
    # The durable owner was authorized before deletion; retry must not depend
    # on a conversation row that may already be gone after interrupted cleanup.
    await stores.pending_effects.enqueue(
        PendingControlPlaneEffect(
            id="interrupted-session-erasure",
            kind="erase_memory_session",
            payload={"owner": asdict(row.owner), "memories_required": True, "index_required": True},
        )
    )
    assert await recover(stores) == 1
    assert not stores.memories.rows
    assert stores.index.deleted == [row.memory_id]


@pytest.mark.asyncio
async def test_session_recovery_also_removes_facts_extracted_from_the_session(stores, monkeypatch):
    row = memory("memory-1", scope=MemoryScope.SESSION, session_id="session-1")
    sourced = memory("memory-sourced", source_session_id="session-1")
    identity = ConversationIdentity(
        row.owner.tenant_id, row.owner.principal_id, "session-1", row.owner.user_id
    )
    await stores.conversations.create(identity, kind="chat")
    for item in (row, sourced):
        await stores.memories.save(item)
    search = stores.memories.search
    monkeypatch.setattr(stores.memories, "search", AsyncMock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        await administration(stores).erase_session(row.owner, actor="test")
    monkeypatch.setattr(stores.memories, "search", search)

    assert await recover(stores) == 1

    assert not stores.memories.rows
    assert sorted(stores.index.deleted) == ["memory-1", "memory-sourced"]


@pytest.mark.asyncio
async def test_a_failed_index_delete_is_reported_as_deferred(stores):
    first, second = memory("memory-1"), memory("memory-2")
    for item in (first, second):
        await stores.memories.save(item)
    journal = MemoryErasureJournal(stores.memories, stores.index, stores.pending_effects)

    stores.index.failure = RuntimeError("index unavailable")
    assert await journal.erase_memory(first) == ErasedMemory(points=0, deferred=True)
    stores.index.failure = None
    assert await journal.erase_memory(second) == ErasedMemory(points=1, deferred=False)


async def _seed_session(stores, row):
    identity = ConversationIdentity(
        row.owner.tenant_id, row.owner.principal_id, "session-1", row.owner.user_id
    )
    await stores.conversations.create(identity, kind="chat")
    return identity


@pytest.mark.asyncio
async def test_other_principals_rows_never_crowd_out_the_callers_row(stores, monkeypatch):
    monkeypatch.setattr(administration_module, "_PAGE", 2)
    mine = memory("mine", source_session_id="session-1", importance=0.1)
    await _seed_session(stores, mine)
    await stores.memories.save(mine)
    for index in range(5):
        await stores.memories.save(
            memory(
                f"theirs-{index}",
                principal_id="another-caller",
                source_session_id="session-1",
                importance=0.9,
            )
        )

    await administration(stores).erase_session(
        replace(mine.owner, session_id="session-1"), actor="test"
    )

    assert "mine" not in stores.memories.rows
    assert len(stores.memories.rows) == 5


@pytest.mark.asyncio
async def test_an_incomplete_session_purge_stays_pending_and_is_not_a_success(stores, monkeypatch):
    row = memory("memory-1", source_session_id="session-1")
    identity = await _seed_session(stores, row)
    await stores.memories.save(row)
    service = administration(stores)
    monkeypatch.setattr(
        service, "_purge_session", AsyncMock(return_value=PurgeTotals(complete=False))
    )

    with pytest.raises(HarborUnavailableError):
        await service.erase_session(replace(row.owner, session_id="session-1"), actor="test")

    assert await stores.conversations.exists(identity)
    assert len(stores.pending_effects.effects) == 1


@pytest.mark.asyncio
async def test_an_expired_sourced_fact_is_removed_by_a_conversation_delete(stores):
    past = utc_now() - timedelta(days=2)
    row = memory("expired", source_session_id="session-1", updated_at=past)
    row = replace(row, expires_at=past + timedelta(days=1))
    await _seed_session(stores, row)
    await stores.memories.save(row)

    await administration(stores).erase_session(
        replace(row.owner, session_id="session-1"), actor="test"
    )

    assert not stores.memories.rows


@pytest.mark.asyncio
async def test_purge_totals_count_deferred_vector_deletes(stores):
    row = memory("memory-1", source_session_id="session-1")
    await stores.memories.save(row)
    stores.index.failure = RuntimeError("index unavailable")

    totals = await administration(stores)._purge(
        row.owner, (), stored_by_owner=True, source_session_id="session-1"
    )

    assert (totals.memories, totals.index_points, totals.deferred) == (1, 0, 1)
