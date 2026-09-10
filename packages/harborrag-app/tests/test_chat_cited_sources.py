"""Citations report the sources the answer used, not the ones retrieval returned.

Retrieval always hands back its top-k, so every answer came back with a full
set of citations -- including "Your name is Huy.", which used none of them. A
client rendering that list shows five unrelated documents as the evidence for
an answer that never touched them.

The model is already told to cite what it uses as ``[Source N]``, and it does:
a corpus question marks four sources, a conversational one marks none.
"""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control.chat.presenters import cited_results
from harborrag_core.domain.retrieval import RetrievalResult


def _results(count: int) -> tuple[RetrievalResult, ...]:
    return tuple(
        RetrievalResult(
            id=f"chunk-{index}",
            text=f"body {index}",
            score=0.9,
            metadata={"document_id": f"doc-{index}"},
        )
        for index in range(1, count + 1)
    )


def test_only_the_marked_sources_are_returned() -> None:
    used = cited_results("Grounded in [Source 1] and [Source 3].", _results(4))

    assert [result.id for result in used] == ["chunk-1", "chunk-3"]


def test_an_answer_that_marks_nothing_cites_nothing() -> None:
    """The 'what is my name?' case: answered from the conversation."""

    assert cited_results("Your name is Huy.", _results(5)) == ()


def test_markers_are_deduplicated_and_kept_in_retrieval_order() -> None:
    """A source cited three times is still one citation, ordered by retrieval."""

    used = cited_results("[Source 3] then [Source 1] then [Source 3] again.", _results(3))

    assert [result.id for result in used] == ["chunk-1", "chunk-3"]


@pytest.mark.parametrize("answer", ["See [Source 9].", "See [Source 0].", "See [Source -1]."])
def test_out_of_range_markers_are_ignored(answer: str) -> None:
    """A hallucinated index must not raise and must not cite the wrong document."""

    assert cited_results(answer, _results(3)) == ()


def test_no_results_means_no_citations_whatever_the_answer_says() -> None:
    assert cited_results("Confidently citing [Source 1].", ()) == ()


@pytest.mark.asyncio
async def test_completion_reports_only_the_sources_the_answer_cited() -> None:
    """End to end: the response's citations match the answer, not the retrieval."""

    from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

    from harborrag_app.workflow_control.chat import (
        ChatApplicationService,
        ChatExecutionOptions,
    )
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

    class _CitingChat(FakeChatFacade):
        async def complete(self, request, *, prompt=None):
            response = await super().complete(request, prompt=prompt)
            return response.model_copy(
                update={
                    "message": response.message.model_copy(
                        update={"content": "Grounded in [Source 2]."}
                    )
                }
            )

    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    memory = InMemoryConversationMemory()
    await memory.create(identity, kind="chat")
    service = ChatApplicationService(
        lambda: FakeRuntime(_CitingChat(), FakeRetrievalFacade(_results(3))),  # type: ignore[arg-type]
        RuntimeSettings(),
        memory=memory,
    )

    response = await service.complete(
        "What does the policy say?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1"),
    )

    assert [c["chunk_id"] for c in response.data["citations"]] == ["chunk-2"]


@pytest.mark.asyncio
async def test_stream_ends_with_the_sources_the_answer_cited() -> None:
    """A stream cannot gate the first frame, so it corrects itself at the end.

    The opening ``citations`` event is emitted before any text exists, so it
    can only ever be "what retrieval found". A second one after the answer
    carries what was actually used, giving a client that overwrites its
    sources panel the same meaning the JSON field has.
    """

    from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

    from harborrag_app.workflow_control.chat import (
        ChatApplicationService,
        ChatExecutionOptions,
    )
    from harborrag_core.models.chat import HarborChatStreamChunk
    from harborrag_core.models.chat.enums import StreamEventType
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

    class _CitingStream(FakeChatFacade):
        async def _events(self):
            yield HarborChatStreamChunk(
                event=StreamEventType.TEXT_DELTA,
                logical_model="primary",
                provider="mock",
                provider_model="mock-chat",
                deployment="internal-deployment",
                text_delta="Grounded in [Source 2].",
            )
            yield HarborChatStreamChunk(
                event=StreamEventType.COMPLETED,
                logical_model="primary",
                provider="mock",
                provider_model="mock-chat",
                deployment="internal-deployment",
                finish_reason="stop",
            )

    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    memory = InMemoryConversationMemory()
    await memory.create(identity, kind="chat")
    service = ChatApplicationService(
        lambda: FakeRuntime(_CitingStream(), FakeRetrievalFacade(_results(3))),  # type: ignore[arg-type]
        RuntimeSettings(),
        memory=memory,
    )

    events = [
        event
        async for event in service.stream(
            "What does the policy say?",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1"),
        )
    ]

    kinds = [event["kind"] for event in events]
    # The opening frame keeps its meaning: everything retrieval found.
    assert kinds[0] == "citations"
    opening = events[0]
    assert [c["chunk_id"] for c in opening["citations"]] == ["chunk-1", "chunk-2", "chunk-3"]
    # The closing frame is a distinct kind, so a client that appends rather
    # than overwrites cannot mistake one for the other.
    assert kinds[-1] == "cited_sources"
    assert [c["chunk_id"] for c in events[-1]["citations"]] == ["chunk-2"]
