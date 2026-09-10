"""The in-memory conversation history must satisfy the same ownership contract.

``InMemoryConversationMemory`` backs unit tests and local runs, so its
predicates have to be user-scoped exactly like the SQL adapter's -- otherwise
a test suite would prove isolation that production does not have.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harborrag_runtime.memory import (
    ConversationHistoryRepository,
    ConversationIdentity,
    ConversationMessage,
    ConversationTurn,
    InMemoryConversationMemory,
    encode_conversation_cursor,
)

pytestmark = pytest.mark.unit

OWNER = ConversationIdentity("tenant", "shared-principal", "session-1", "user-1")
INTRUDER = ConversationIdentity("tenant", "shared-principal", "session-1", "user-2")


def _message(message_id: str) -> ConversationMessage:
    return ConversationMessage(message_id, "user", "q", datetime.now(UTC))


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_the_in_memory_history_still_satisfies_the_repository_protocol() -> None:
    repository: ConversationHistoryRepository = InMemoryConversationMemory()

    page = await repository.list_conversations(tenant_id="tenant", user_id="user-1")
    assert page.conversations == ()
    assert page.next_cursor is None


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_a_second_user_cannot_reach_the_first_users_session() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(OWNER, title="  Owner's chat  ")
    await memory.append(OWNER, ConversationTurn("private", "answer"))

    assert await memory.exists(INTRUDER) is False
    assert await memory.recent(INTRUDER) == ()
    assert await memory.rename_conversation(INTRUDER, title="hijacked") is False
    assert (
        await memory.list_conversations(tenant_id="tenant", user_id="user-2")
    ).conversations == (())

    owner_page = await memory.list_conversations(tenant_id="tenant", user_id="user-1")
    assert [row.title for row in owner_page.conversations] == ["Owner's chat"]
    assert [row.message_count for row in owner_page.conversations] == [2]


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_listing_orders_by_activity_filters_kind_and_pages() -> None:
    memory = InMemoryConversationMemory()
    for index, kind in enumerate(("chat", "chat", "agent")):
        identity = ConversationIdentity("tenant", "p", f"session-{index}", "user-1")
        await memory.create(identity, kind=kind)  # type: ignore[arg-type]
        await memory.append_messages(identity, (_message(f"m-{index}"),))

    page = await memory.list_conversations(tenant_id="tenant", user_id="user-1")
    ordered = [row.session_id for row in page.conversations]
    assert ordered == ["session-2", "session-1", "session-0"]
    assert page.next_cursor is None

    chats = await memory.list_conversations(tenant_id="tenant", user_id="user-1", kind="chat")
    assert [row.session_id for row in chats.conversations] == ["session-1", "session-0"]

    first = await memory.list_conversations(tenant_id="tenant", user_id="user-1", limit=2)
    assert [row.session_id for row in first.conversations] == ["session-2", "session-1"]
    assert first.next_cursor is not None
    second = await memory.list_conversations(
        tenant_id="tenant", user_id="user-1", limit=2, cursor=first.next_cursor
    )
    assert [row.session_id for row in second.conversations] == ["session-0"]
    assert second.next_cursor is None


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_a_malformed_or_foreign_cursor_is_rejected() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(OWNER)
    await memory.create(ConversationIdentity("tenant", "p", "stranger", "user-2"))

    with pytest.raises(ValueError, match="unknown conversation cursor"):
        await memory.list_conversations(tenant_id="tenant", user_id="user-1", cursor="!!not-b64!!")
    with pytest.raises(ValueError, match="unknown conversation cursor"):
        await memory.list_conversations(
            tenant_id="tenant",
            user_id="user-1",
            cursor=encode_conversation_cursor("stranger"),
        )


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_renaming_trims_bounds_and_clears_blank_titles() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(OWNER)

    assert await memory.rename_conversation(OWNER, title="  Renamed  ") is True
    page = await memory.list_conversations(tenant_id="tenant", user_id="user-1")
    assert page.conversations[0].title == "Renamed"

    assert await memory.rename_conversation(OWNER, title="t" * 500) is True
    page = await memory.list_conversations(tenant_id="tenant", user_id="user-1")
    title = page.conversations[0].title
    assert title is not None
    assert len(title) == 200

    assert await memory.rename_conversation(OWNER, title="   ") is True
    page = await memory.list_conversations(tenant_id="tenant", user_id="user-1")
    assert page.conversations[0].title is None


@pytest.mark.asyncio
@pytest.mark.graybox
async def test_appending_messages_bumps_the_activity_timestamp() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(OWNER)
    page = await memory.list_conversations(tenant_id="tenant", user_id="user-1")
    created = page.conversations[0].updated_at

    await memory.append(OWNER, ConversationTurn("q", "a"))
    page = await memory.list_conversations(tenant_id="tenant", user_id="user-1")
    assert page.conversations[0].updated_at > created
    assert page.conversations[0].created_at == created
