"""Public context retains three complete exchanges without auxiliary model calls."""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control.memory.context import recent_memory_context
from harborrag_app.workflow_control.memory.identity import MemoryIdentity
from harborrag_app.workflow_control.memory.messages import answer_message, question_message
from harborrag_runtime.memory import InMemoryConversationMemory

IDENTITY = MemoryIdentity("ACME", "reader", "alice", "session")


@pytest.mark.asyncio
async def test_only_latest_three_complete_exchanges_enter_context() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY.conversation())
    for index in range(5):
        await memory.append_messages(
            IDENTITY.conversation(),
            (question_message(f"question-{index}"), answer_message(f"answer-{index}")),
        )

    context = await recent_memory_context(memory, IDENTITY, "and next?")

    assert [message.content for message in context.messages] == [
        "question-2",
        "answer-2",
        "question-3",
        "answer-3",
        "question-4",
        "answer-4",
    ]
    assert len(await memory.recent_messages(IDENTITY.conversation(), limit=20)) == 10
    assert context.standalone_query == "and next?"
    assert context.summary is None
    assert context.recalled == ()
    assert context.rewritten is False
    assert context.summary_written is False


@pytest.mark.asyncio
async def test_partial_and_unmatched_messages_do_not_displace_completed_exchanges() -> None:
    memory = InMemoryConversationMemory(max_messages=100)
    await memory.create(IDENTITY.conversation())
    complete = (question_message("complete question"), answer_message("complete answer"))
    await memory.append_messages(IDENTITY.conversation(), complete)
    for index in range(20):
        await memory.append_messages(
            IDENTITY.conversation(),
            (question_message(f"interrupted-{index}"), answer_message("partial", partial=True)),
        )
    await memory.append_messages(IDENTITY.conversation(), (question_message("unanswered"),))

    context = await recent_memory_context(memory, IDENTITY, "now")

    assert context.messages == complete


@pytest.mark.asyncio
async def test_empty_history_needs_no_memory_model() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY.conversation())
    context = await recent_memory_context(memory, IDENTITY, "first question")
    assert context.messages == ()
    assert context.standalone_query == "first question"


@pytest.mark.asyncio
async def test_history_failure_returns_empty_context_without_logging_content(caplog) -> None:
    class BrokenMemory(InMemoryConversationMemory):
        async def recent_complete_messages(self, identity, *, limit=3):
            raise RuntimeError("private message content")

    context = await recent_memory_context(BrokenMemory(), IDENTITY, "secret question")
    assert context.messages == ()
    assert context.standalone_query == "secret question"
    assert "Conversation context unavailable" in caplog.text
    assert "private message content" not in caplog.text
    assert "secret question" not in caplog.text
