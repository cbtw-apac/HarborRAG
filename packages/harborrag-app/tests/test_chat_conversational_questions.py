"""A question the conversation already answered must not be refused.

"My name is A." then "What is my name?" returned something unhelpful: the
final user turn instructed the model to answer from retrieved context and to
say that context was insufficient, and retrieval -- which always returns its
top matches -- had supplied chunks about unrelated documents. The answer was
one turn above, and the prompt told the model not to use it.
"""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")

# What retrieval returns for a personal question against a corpus about
# something else -- the ordinary case, not a contrived one.
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
    await memory.create(IDENTITY, kind="chat")
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
            options=ChatExecutionOptions(session_id="session-1"),
        )
    return chat


@pytest.mark.asyncio
async def test_follow_up_is_not_scoped_away_from_the_conversation() -> None:
    chat = await _ask("My name is A.", "What is my name?")
    final = chat.requests[1].messages[-1].content

    # The earlier turn is still replayed as its own message...
    assert any("My name is A." in message.content for message in chat.requests[1].messages[:-1])
    # ...and nothing in the final turn tells the model to disregard it.
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
