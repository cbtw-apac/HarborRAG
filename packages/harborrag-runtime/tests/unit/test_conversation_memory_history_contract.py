"""In-memory and database-wrapped history: the session and message fields chat history reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from harborrag_runtime.memory import (
    ConversationHistoryRepository,
    ConversationIdentity,
    ConversationMessage,
    DatabaseConversationMemory,
    InMemoryConversationMemory,
)

pytestmark = pytest.mark.unit


def _identity(session_id: str, user_id: str = "alice") -> ConversationIdentity:
    return ConversationIdentity("ACME", "reader-1", session_id, user_id)


def _message(message_id: str, role: str = "user", *, partial: bool = False) -> ConversationMessage:
    return ConversationMessage(
        message_id=message_id,
        role=role,  # type: ignore[arg-type]
        content=f"text of {message_id}",
        created_at=datetime.now(UTC),
        partial=partial,
    )


def _stores() -> list[ConversationHistoryRepository]:
    memory = InMemoryConversationMemory()
    wrapped: Any = DatabaseConversationMemory(repository=InMemoryConversationMemory(), engine=None)  # type: ignore[arg-type]
    return [memory, wrapped]


@pytest.mark.asyncio
@pytest.mark.parametrize("store", _stores(), ids=["in-memory", "database-wrapper"])
async def test_a_session_keeps_its_project_and_reads_back_as_a_summary(
    store: ConversationHistoryRepository,
) -> None:
    owner = _identity("ses_1")
    await store.create(owner, title="Go candidates", project_id="cpm")

    summary = await store.get_conversation(owner)

    assert summary is not None
    assert (summary.session_id, summary.title, summary.project_id) == (
        "ses_1",
        "Go candidates",
        "cpm",
    )
    assert summary.message_count == 0
    assert await store.get_conversation(_identity("ses_1", "bob")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("store", _stores(), ids=["in-memory", "database-wrapper"])
async def test_history_pages_newest_first_without_partial_or_system_messages(
    store: ConversationHistoryRepository,
) -> None:
    owner = _identity("ses_1")
    await store.create(owner)
    await store.append_messages(
        owner,
        (
            _message("msg_1"),
            _message("msg_2", "assistant"),
            _message("msg_3", "system"),
            _message("msg_4"),
            _message("msg_5", "assistant", partial=True),
            _message("msg_6"),
            _message("msg_7", "assistant"),
        ),
    )

    first = await store.history_page(owner, before_message_id=None, limit=3)
    second = await store.history_page(owner, before_message_id=first[-1].message_id, limit=3)
    summary = await store.get_conversation(owner)

    assert [message.message_id for message in first] == ["msg_7", "msg_6", "msg_4"]
    assert [message.message_id for message in second] == ["msg_2", "msg_1"]
    assert summary is not None
    assert summary.message_count == 5
    with pytest.raises(ValueError, match="cursor"):
        await store.history_page(owner, before_message_id="msg_5", limit=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("store", _stores(), ids=["in-memory", "database-wrapper"])
async def test_titles_are_searched_case_insensitively_and_literally(
    store: ConversationHistoryRepository,
) -> None:
    for session_id, title in (
        ("ses_1", "Go candidates in Hanoi"),
        ("ses_2", "Rust contractors"),
        ("ses_3", "100%_literal"),
        ("ses_4", None),
    ):
        await store.create(_identity(session_id), title=title)
    await store.create(_identity("ses_5", "bob"), title="Hanoi for Bob")

    async def titles(query: str) -> list[str | None]:
        page = await store.list_conversations(tenant_id="ACME", user_id="alice", title_query=query)
        return [row.title for row in page.conversations]

    assert await titles("HANOI") == ["Go candidates in Hanoi"]
    assert await titles("%") == ["100%_literal"]
    assert await titles("_") == ["100%_literal"]
