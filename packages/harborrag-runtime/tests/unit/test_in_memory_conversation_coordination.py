"""The local conversation adapter mirrors title and idempotency semantics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage
from harborrag_runtime.memory import InMemoryConversationMemory

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.asyncio
async def test_title_generation_preserves_manual_changes_across_credentials() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    rotated = ConversationIdentity("tenant", "other-principal", "session", "user")
    await memory.create(identity)
    assert await memory.exists(rotated)
    claims = await asyncio.gather(
        memory.set_generated_title(identity, title="One"),
        memory.set_generated_title(rotated, title="Two"),
    )
    assert sum(claims) == 1
    assert await memory.rename_conversation(rotated, title="")
    assert not await memory.set_generated_title(identity, title="Ignored")
    assert await memory.get_title(identity) is None


@pytest.mark.asyncio
async def test_request_claims_replay_success_and_preserve_failures() -> None:
    memory = InMemoryConversationMemory()
    request = {"tenant_id": "tenant", "user_id": "user", "key": "key", "request_hash": "hash"}
    assert (await memory.claim_completion(**request)).status == "claimed"
    assert (await memory.claim_completion(**request)).status == "in_progress"
    assert (
        await memory.claim_completion(**{**request, "request_hash": "other"})
    ).status == "conflict"
    await memory.finish_completion(**request, response_json='{"answer":"ok"}')
    assert (await memory.claim_completion(**request)).result_json == '{"answer":"ok"}'
    await memory.finish_completion(**request, response_json=None)
    assert (await memory.claim_completion(**request)).status == "completed"
    failure = {**request, "key": "failed"}
    assert (await memory.claim_completion(**failure)).status == "claimed"
    await memory.finish_completion(**failure, response_json=None)
    assert (await memory.claim_completion(**failure)).status == "failed"


@pytest.mark.asyncio
async def test_deleting_a_session_clears_replay_content_and_retains_the_claim() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    request = {"tenant_id": "tenant", "user_id": "user", "key": "key", "request_hash": "hash"}
    await memory.create(identity)
    await memory.claim_completion(**request)
    await memory.finish_completion(**request, session_id="session", response_json="private answer")
    await memory.delete(identity)
    claim = await memory.claim_completion(**request)
    assert claim.status == "failed"
    assert claim.result_json is None


@pytest.mark.asyncio
async def test_recent_complete_messages_excludes_tool_calls_and_unfinished_turns() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await memory.create(identity)
    now = datetime.now(UTC)
    messages = (
        ConversationMessage("u1", "user", "first question", now),
        ConversationMessage("call", "assistant", "Searching", now, tool_calls_json="[{}]"),
        ConversationMessage("tool", "tool", "evidence", now),
        ConversationMessage("a1", "assistant", "complete answer", now),
        ConversationMessage("u2", "user", "second question", now),
        ConversationMessage("a2", "assistant", "partial answer", now, partial=True),
        ConversationMessage("u3", "user", "unanswered", now),
    )
    await memory.append_messages(identity, messages)
    assert [message.message_id for message in await memory.recent_complete_messages(identity)] == [
        "u1",
        "a1",
    ]
