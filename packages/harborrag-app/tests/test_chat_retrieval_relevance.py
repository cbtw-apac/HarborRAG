"""Only results that are actually relevant reach the prompt and the citations.

Chat retrieval returns its top-k regardless of quality, so a personal or
off-topic question came back with five unrelated documents: injected as
evidence, and reported to the caller as citations for an answer that used
none of them. ``relevance`` is the first signal that can tell them apart --
``score`` cannot, because on the hybrid lane it is rank arithmetic.
"""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")

RESULTS = (
    RetrievalResult(
        id="on-topic",
        text="Release approvals require two reviewers.",
        score=0.87,
        relevance=0.81,
        metadata={"document_id": "release-policy"},
    ),
    RetrievalResult(
        id="off-topic",
        text="The activity timeout is 30 seconds.",
        score=0.99,  # ranked first by fusion, but barely similar
        relevance=0.52,
        metadata={"document_id": "ops-runbook"},
    ),
)


async def _complete(min_relevance: float) -> tuple[FakeChatFacade, dict]:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    chat = FakeChatFacade()
    service = ChatApplicationService(
        lambda: FakeRuntime(chat, FakeRetrievalFacade(RESULTS)),  # type: ignore[arg-type]
        RuntimeSettings(chat_retrieval_min_relevance=min_relevance),
        memory=memory,
    )
    response = await service.complete(
        "What does the release policy say?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1"),
    )
    return chat, response.data


@pytest.mark.asyncio
async def test_below_threshold_results_reach_neither_prompt_nor_citations() -> None:
    chat, data = await _complete(0.6)
    prompt = chat.requests[0].messages[-1].content

    assert "Release approvals require two reviewers." in prompt
    assert "The activity timeout is 30 seconds." not in prompt
    # Never offered to the model, so it can never become a citation either.
    assert "[Source 2]" not in prompt


@pytest.mark.asyncio
async def test_threshold_of_zero_keeps_every_result() -> None:
    """The default must not silently change what existing deployments retrieve."""

    chat, _ = await _complete(0.0)
    prompt = chat.requests[0].messages[-1].content

    assert "Release approvals require two reviewers." in prompt
    assert "The activity timeout is 30 seconds." in prompt


@pytest.mark.asyncio
async def test_all_results_filtered_falls_back_to_the_no_sources_prompt() -> None:
    """Nothing relevant is the same situation as nothing retrieved."""

    chat, _ = await _complete(0.95)
    prompt = chat.requests[0].messages[-1].content

    assert "No sources were retrieved for this question." in prompt
