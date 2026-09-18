"""Guarantees the memory tiers state but did not previously keep."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from harborrag_core.ports.conversation import ConversationMessage
from harborrag_memory import MemoryManager, MemoryOwner
from harborrag_memory.errors import MemoryConfigurationError, MemoryScopeError
from harborrag_memory.identity import conversation_identity
from harborrag_memory.langchain.converters import to_langchain_message
from harborrag_memory.tiers.working import InMemoryWorkingMemoryStore

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _run_owner(run: str) -> MemoryOwner:
    return MemoryOwner(
        tenant_id="T",
        principal_id="P",
        user_id="U",
        session_id="S",
        run_id=run,
    )


@pytest.mark.asyncio
async def test_expired_scratch_state_does_not_outlive_the_process() -> None:
    """Eviction happened in ``get``, and only for the owner being read.

    A run that ended without one more read kept its scratch state forever, so a
    long-lived worker accumulated one dead entry per run.
    """

    store = InMemoryWorkingMemoryStore()
    for index in range(5):
        await store.put(_run_owner(f"run-{index}"), {"n": index}, ttl_seconds=1)
    await asyncio.sleep(1.05)

    await store.put(_run_owner("run-live"), {"n": "live"}, ttl_seconds=60)

    assert len(store._values) == 1
    assert await store.get(_run_owner("run-0")) is None
    assert await store.get(_run_owner("run-live")) == {"n": "live"}


@pytest.mark.asyncio
async def test_a_snapshot_that_cannot_answer_says_so() -> None:
    """Returning () made "nothing stored" and "not configured" identical."""

    owner = MemoryOwner(tenant_id="T", principal_id="P", user_id="U", session_id="S")
    manager = MemoryManager()

    from harborrag_core.ports.memory import MemoryQuery

    with pytest.raises(MemoryConfigurationError, match="long-term memory is not configured"):
        await manager.snapshot(owner, query=MemoryQuery(owner=owner))

    empty = await manager.snapshot(owner)
    assert empty.memories == ()


def test_a_corrupt_stored_column_costs_one_message_not_the_conversation() -> None:
    """One unparseable row used to raise out of reading the whole history."""

    message = ConversationMessage(
        message_id="m-1",
        role="assistant",
        content="the answer",
        created_at=NOW,
        citations_json="{not json",
        tool_calls_json="{also not json",
    )

    converted = to_langchain_message(message)

    assert converted.content == "the answer"
    assert "citations" not in converted.additional_kwargs


def test_one_rule_decides_who_owns_a_conversation() -> None:
    """Three copies of this derivation existed, comment and all."""

    owner = MemoryOwner(tenant_id="T", principal_id="P", user_id="U", session_id="S")
    credential_only = MemoryOwner(tenant_id="T", principal_id="P", session_id="S")

    assert conversation_identity(owner, subject="x").user_id == "U"
    assert conversation_identity(credential_only, subject="x").user_id == "P"

    with pytest.raises(MemoryScopeError, match="requires principal_id and session_id"):
        conversation_identity(MemoryOwner(tenant_id="T"), subject="x")
