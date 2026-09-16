"""Citations report the sources the answer used, not the ones retrieval returned.

Retrieval always hands back its top-k, so every answer came back with a full
set of citations -- including "Your name is Huy.", which used none of them. A
client rendering that list shows five unrelated documents as the evidence for
an answer that never touched them.

The model is already told to cite what it uses as ``[Source N]``, and it does:
a corpus question marks four sources, a conversational one marks none.
"""

from __future__ import annotations

import json

import pytest

from harborrag_app.workflow_control.chat.presenters import (
    citation_data,
    citation_marker,
    cited_results,
)
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
    results = _results(4)
    answer = f"Grounded in {citation_marker(1, results[0])} and {citation_marker(3, results[2])}."
    used = cited_results(answer, results)

    assert [result.id for result in used] == ["chunk-1", "chunk-3"]


def test_only_the_server_generated_readable_marker_is_accepted() -> None:
    results = list(_results(3))
    results[1].metadata.update(
        {
            "document_title": "Deployment Guide",
            "section_path": ["Operations", "Rollback policy"],
        }
    )
    marker = citation_marker(2, results[1])

    used = cited_results(f"Supported {marker}; forged [Source 1: Overview].", results)

    assert [result.id for result in used] == ["chunk-2"]


def test_an_answer_that_marks_nothing_cites_nothing() -> None:
    """The 'what is my name?' case: answered from the conversation."""

    assert cited_results("Your name is Huy.", _results(5)) == ()


def test_legacy_plain_marker_is_not_readable_provenance() -> None:
    assert cited_results("Grounded in [Source 1].", _results(1)) == ()


def test_markers_are_deduplicated_and_kept_in_retrieval_order() -> None:
    """A source cited three times is still one citation, ordered by retrieval."""

    results = _results(3)
    first = citation_marker(1, results[0])
    third = citation_marker(3, results[2])
    used = cited_results(f"{third} then {first} then {third} again.", results)

    assert [result.id for result in used] == ["chunk-1", "chunk-3"]


@pytest.mark.parametrize("answer", ["See [Source 9].", "See [Source 0].", "See [Source -1]."])
def test_out_of_range_markers_are_ignored(answer: str) -> None:
    """A hallucinated index must not raise and must not cite the wrong document."""

    assert cited_results(answer, _results(3)) == ()


def test_no_results_means_no_citations_whatever_the_answer_says() -> None:
    assert cited_results("Confidently citing [Source 1].", ()) == ()


def test_oversized_source_number_is_ignored_without_raising() -> None:
    answer = f"Unsupported [Source {'9' * 5_000}]."

    assert cited_results(answer, _results(1)) == ()


def test_citation_data_includes_readable_document_section_and_location() -> None:
    result = RetrievalResult(
        id="chunk-1",
        text="body",
        score=0.9,
        metadata={
            "document_id": "doc-1",
            "document_title": "Deployment Guide",
            "section_path": ["Operations", "Rollback policy"],
            "citation_locator": {"start_line": 40, "end_line": 46},
        },
    )

    assert citation_data(result) == {
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "score": 0.9,
        "document_title": "Deployment Guide",
        "section_path": ("Operations", "Rollback policy"),
        "location": "lines 40–46",
    }
    assert citation_marker(1, result) == (
        '[Source 1: "Deployment Guide" — Operations > Rollback policy]'
    )


def test_public_readable_metadata_is_bounded_and_control_characters_are_removed() -> None:
    result = RetrievalResult(
        id="chunk-1",
        text="body",
        score=0.9,
        metadata={
            "document_id": "doc-1",
            "document_title": "\u202e" + "T" * 1_000,
            "section_path": ["\u2066" + "S" * 1_000] * 100,
        },
    )

    citation = citation_data(result)

    assert len(citation["document_title"]) == 256
    assert "\u202e" not in citation["document_title"]
    assert len(citation["section_path"]) == 16
    assert all(len(part) == 128 for part in citation["section_path"])


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
            results = _results(3)
            return response.model_copy(
                update={
                    "message": response.message.model_copy(
                        update={"content": f"Grounded in {citation_marker(2, results[1])}."}
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
        options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
    )

    assert [c["chunk_id"] for c in response.data["citations"]] == ["chunk-2"]
    messages = await memory.recent_messages(identity, limit=2)
    assert json.loads(messages[-1].citations_json) == list(response.data["citations"])


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
            results = _results(3)
            yield HarborChatStreamChunk(
                event=StreamEventType.TEXT_DELTA,
                logical_model="primary",
                provider="mock",
                provider_model="mock-chat",
                deployment="internal-deployment",
                text_delta=f"Grounded in {citation_marker(2, results[1])}.",
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
            options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
        )
    ]

    kinds = [event["kind"] for event in events]
    # The opening frame keeps its meaning: everything retrieval found.
    assert kinds[0] == "citations"
    opening = events[0]
    assert [c["chunk_id"] for c in opening["citations"]] == ["chunk-1", "chunk-2", "chunk-3"]
    # The closing frame is a distinct kind, so a client that appends rather
    # than overwrites cannot mistake one for the other.
    assert kinds[-2:] == ["cited_sources", "result"]
    assert [c["chunk_id"] for c in events[-2]["citations"]] == ["chunk-2"]
    assert events[-1]["result"]["citations"] == events[-2]["citations"]
