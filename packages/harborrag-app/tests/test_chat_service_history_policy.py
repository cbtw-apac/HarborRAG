"""History-aware retrieval: the standalone query, not the raw follow-up, is searched."""

from __future__ import annotations

import pytest
from chat_service_fixtures import (
    FakeChatFacade,
    FakeMemoryFacade,
    FakeRetrievalFacade,
    FakeRuntime,
    replayed,
)

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import (
    ConversationIdentity,
    InMemoryConversationMemory,
    MemoryContext,
    MemoryContextRequest,
    MemoryPolicy,
)

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
OPTIONS = ChatExecutionOptions(session_id="session-1")


class RewritingMemoryFacade(FakeMemoryFacade):
    """Stands in for a memory model that condenses the follow-up."""

    def __init__(self, standalone_query: str) -> None:
        super().__init__()
        self.standalone_query = standalone_query

    async def build_context(
        self,
        request: MemoryContextRequest,
        *,
        messages: object,
        memories: object = None,
        index: object = None,
    ) -> MemoryContext:
        context = await super().build_context(
            request, messages=messages, memories=memories, index=index
        )
        return MemoryContext(
            messages=context.messages,
            summary="Earlier: the release policy was discussed.",
            recalled=(),
            standalone_query=self.standalone_query,
            rewritten=True,
            summary_written=False,
        )


class FailingMemoryFacade:
    async def build_context(
        self,
        request: MemoryContextRequest,
        *,
        messages: object,
        memories: object = None,
        index: object = None,
    ) -> MemoryContext:
        del request, messages, memories
        raise RuntimeError("memory store is down")


def _result(text: str) -> RetrievalResult:
    return RetrievalResult(
        id="chunk-1",
        text=text,
        score=0.9,
        metadata={"document_id": "document:handbook"},
    )


async def _service(
    runtime: FakeRuntime,
    memory: InMemoryConversationMemory | None = None,
) -> ChatApplicationService:
    store = memory or InMemoryConversationMemory()
    await store.create(IDENTITY, kind="chat")
    return ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        RuntimeSettings(),
        memory=store,
    )


@pytest.mark.asyncio
async def test_retrieval_searches_the_standalone_query_not_the_raw_follow_up() -> None:
    retrieval = FakeRetrievalFacade((_result("The Platform team owns releases."),))
    runtime = FakeRuntime(
        FakeChatFacade(),
        retrieval,
        memory=RewritingMemoryFacade("Who owns the release policy?"),
    )
    service = await _service(runtime)

    response = await service.complete(
        "and who owns it?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=OPTIONS,
    )

    assert response.ok is True
    assert retrieval.request is not None
    assert retrieval.request.query == "Who owns the release policy?"


@pytest.mark.asyncio
async def test_the_user_still_sees_their_own_question_in_the_prompt() -> None:
    """Rewriting steers retrieval; the model must still answer what was asked."""

    chat = FakeChatFacade()
    runtime = FakeRuntime(
        chat,
        FakeRetrievalFacade((_result("The Platform team owns releases."),)),
        memory=RewritingMemoryFacade("Who owns the release policy?"),
    )
    service = await _service(runtime)

    await service.complete(
        "and who owns it?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=OPTIONS,
    )

    prompt = str(chat.requests[0].messages[-1].content)
    assert "Question: and who owns it?" in prompt
    assert "Earlier: the release policy was discussed." in prompt
    assert chat.requests[0].metadata.retrieval_query == "Who owns the release policy?"


@pytest.mark.asyncio
async def test_retrieval_uses_the_raw_question_when_nothing_was_rewritten() -> None:
    retrieval = FakeRetrievalFacade((_result("It ships weekly."),))
    runtime = FakeRuntime(FakeChatFacade(), retrieval)
    service = await _service(runtime)

    await service.complete(
        "What is the release policy?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=OPTIONS,
    )

    assert retrieval.request is not None
    assert retrieval.request.query == "What is the release policy?"


@pytest.mark.asyncio
async def test_a_memory_failure_answers_from_retrieval_instead_of_failing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    retrieval = FakeRetrievalFacade((_result("It ships weekly."),))
    runtime = FakeRuntime(FakeChatFacade(), retrieval)
    runtime.memory = FailingMemoryFacade()  # type: ignore[assignment]
    service = await _service(runtime)

    response = await service.complete(
        "What is the release policy?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=OPTIONS,
    )

    assert response.ok is True
    assert retrieval.request is not None
    assert retrieval.request.query == "What is the release policy?"
    assert any("answering from retrieval alone" in record.getMessage() for record in caplog.records)
    assert "What is the release policy?" not in caplog.text


@pytest.mark.asyncio
async def test_the_window_is_replayed_across_turns_in_order() -> None:
    chat = FakeChatFacade()
    runtime = FakeRuntime(
        chat,
        memory=FakeMemoryFacade(MemoryPolicy(recent_max_messages=6, summary_keep_messages=6)),
    )
    service = await _service(runtime)

    for question in ("First", "Second", "Third"):
        await service.complete(
            question,
            tenant_id="ACME",
            principal_id="reader-1",
            options=OPTIONS,
        )

    third = replayed(chat.requests[2])
    assert third[:4] == ["First", "Hello", "Second", "Hello"]
    assert third[-1] == "Third"
