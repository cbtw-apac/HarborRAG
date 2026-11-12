"""A question the conversation already answered must not be refused.

"My name is A." then "What is my name?" returned something unhelpful: the
final user turn instructed the model to answer from retrieved context and to
say that context was insufficient, and retrieval -- which always returns its
top matches -- had supplied chunks about unrelated documents. The answer was
one turn above, and the prompt told the model not to use it.
"""

from __future__ import annotations

import pytest

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory
from harborrag_runtime.application.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity
from harborrag_runtime.testing.chat_fakes import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

pytestmark = pytest.mark.unit

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")

UNRELATED = (
    RetrievalResult(
        id="chunk-1",
        text="The activity timeout is 30 seconds.",
        score=0.21,
        metadata={"document_id": "ops-runbook"},
    ),
)


async def _ask(*prompts: str) -> FakeChatFacade:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY)
    chat = FakeChatFacade()
    service = ChatApplicationService(
        lambda: FakeRuntime(chat, FakeRetrievalFacade(UNRELATED)),  # type: ignore[arg-type]
        RuntimeSettings(),
        memory=memory,
    )
    for prompt in prompts:
        await service.complete(
            prompt,
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
        )
    return chat


@pytest.mark.asyncio
async def test_follow_up_is_not_scoped_away_from_the_conversation() -> None:
    """The earlier turn is still replayed as its own message... ...and nothing in the final turn tells
    the model to disregard it.
    """
    chat = await _ask("My name is A.", "What is my name?")
    final = chat.requests[1].messages[-1].content

    assert any("My name is A." in message.content for message in chat.requests[1].messages[:-1])
    assert "Use the retrieved context below to answer the question" not in final
    assert "Answer from this conversation" in final


@pytest.mark.asyncio
async def test_unrelated_sources_are_marked_as_possibly_irrelevant() -> None:
    """The runbook chunk is offered, but flagged rather than presented as the answer."""

    chat = await _ask("My name is A.", "What is my name?")
    final = chat.requests[1].messages[-1].content

    assert "The activity timeout is 30 seconds." in final
    assert "may be irrelevant" in final
    assert final.rstrip().endswith("Question: What is my name?")
