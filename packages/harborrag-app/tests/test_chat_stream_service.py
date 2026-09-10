"""Application-service tests for streamed retrieval-grounded chat."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from chat_service_fixtures import FakeChatFacade as _ChatFacade
from chat_service_fixtures import FakeRetrievalFacade as _RetrievalFacade
from chat_service_fixtures import FakeRuntime as _Runtime
from chat_service_fixtures import replayed
from test_chat_service import _options
from test_chat_service_memory import _BrokenAppendMemory, _chat_service
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import HarborChatStreamChunk
from harborrag_core.models.chat.enums import StreamEventType
from harborrag_runtime.contracts import RetrievalResponse
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory


def _service(runtime: _Runtime) -> AppService:
    return AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_chat_stream_emits_citations_then_chunks() -> None:
    results = (
        RetrievalResult(
            id="chunk-1",
            text="HarborRAG is a retrieval-augmented generation platform.",
            score=0.9,
            metadata={"document_id": "doc-1"},
        ),
    )
    chat = _ChatFacade()
    service = _service(_Runtime(chat, _RetrievalFacade(results)))
    options = await _options(service)

    events = [
        event
        async for event in service.chat_stream(
            "Hello", tenant_id="ACME", principal_id="reader-1", options=options
        )
    ]

    assert [event["kind"] for event in events] == [
        "citations",
        "chunk",
        "chunk",
        "cited_sources",
    ]
    assert events[0]["citations"] == (
        {"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9},
    )
    assert events[1]["chunk"]["event"] == "text_delta"
    assert events[1]["chunk"]["content"] == "Hello"
    assert chat.request is not None
    assert chat.request.messages[0].content.endswith("Question: Hello")


@pytest.mark.asyncio
async def test_chat_stream_ends_with_error_event_when_provider_stream_fails() -> None:
    chat = _ChatFacade(stream_failure=RuntimeError("secret provider response"))
    service = _service(_Runtime(chat))
    options = await _options(service)

    events = [
        event
        async for event in service.chat_stream(
            "Hello", tenant_id="ACME", principal_id="reader-1", options=options
        )
    ]

    assert [event["kind"] for event in events] == ["citations", "chunk", "error"]
    assert events[-1]["error"] == "RuntimeError"
    assert events[-1]["error_type"] == "RuntimeError"
    assert "secret provider response" not in str(events)


@pytest.mark.asyncio
async def test_chat_stream_recalls_completed_session_history() -> None:
    chat = _ChatFacade()
    service = _service(_Runtime(chat))
    options = await _options(service)

    for message in ("First", "Second"):
        _ = [
            event
            async for event in service.chat_stream(
                message,
                tenant_id="ACME",
                principal_id="reader-1",
                options=options,
            )
        ]

    assert replayed(chat.requests[1]) == [
        "First",
        "Hello",
        "Second",
    ]


@pytest.mark.asyncio
async def test_chat_stream_ends_with_error_event_when_retrieval_fails() -> None:
    class _FailingRetrieval:
        async def search(self, request: object) -> RetrievalResponse:
            del request
            raise RuntimeError("secret provider response")

    runtime = _Runtime(_ChatFacade(), _FailingRetrieval())  # type: ignore[arg-type]
    service = _service(runtime)
    options = await _options(service)
    events = [
        event
        async for event in service.chat_stream(
            "Hello", tenant_id="ACME", principal_id="reader-1", options=options
        )
    ]

    assert events == [{"kind": "error", "error": "RuntimeError", "error_type": "RuntimeError"}]


class _AdapterLikeChat(_ChatFacade):
    """Mimic the provider adapter: yield an ERROR chunk, then raise."""

    async def _events(self) -> AsyncIterator[HarborChatStreamChunk]:
        yield HarborChatStreamChunk(
            event=StreamEventType.TEXT_DELTA,
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            text_delta="Hel",
        )
        yield HarborChatStreamChunk(
            event=StreamEventType.ERROR,
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            error={"type": "ProviderError", "message": "secret provider response"},
        )
        raise RuntimeError("secret provider response")


@pytest.mark.asyncio
async def test_chat_stream_emits_exactly_one_error_when_adapter_yields_error_then_raises() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    await memory.create(identity, kind="chat")
    service = _chat_service(_Runtime(_AdapterLikeChat()), memory)

    events = [
        event
        async for event in service.stream(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1"),
        )
    ]

    assert [event["kind"] for event in events] == ["citations", "chunk", "error"]
    assert events[-1] == {
        "kind": "error",
        "error": "ChatStreamError",
        "error_type": "ChatStreamError",
    }
    assert "secret provider response" not in str(events)
    # The caller saw "Hel" and paid for it, so the turn survives the failure
    # instead of being discarded, with the answer marked as unfinished.
    user, assistant = await memory.recent_messages(identity, limit=10)
    assert (user.role, user.content) == ("user", "Hello")
    assert (assistant.role, assistant.content) == ("assistant", "Hel")
    assert getattr(assistant, "partial", None) is True


@pytest.mark.asyncio
async def test_chat_stream_warns_instead_of_failing_when_memory_append_fails() -> None:
    memory = _BrokenAppendMemory()
    await memory.create(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), kind="chat"
    )
    service = _chat_service(_Runtime(_ChatFacade()), memory)

    events = [
        event
        async for event in service.stream(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1"),
        )
    ]

    assert [event["kind"] for event in events] == [
        "citations",
        "chunk",
        "chunk",
        "cited_sources",
        "warning",
    ]
    assert events[-1]["warning"] == "conversation_memory_unavailable"


@pytest.mark.asyncio
async def test_chat_stream_rejects_an_agent_session() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(
        ConversationIdentity("ACME", "reader-1", "agent-session", "reader-1"), kind="agent"
    )
    service = _chat_service(_Runtime(_ChatFacade()), memory)

    events = [
        event
        async for event in service.stream(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="agent-session"),
        )
    ]

    assert events == [
        {
            "kind": "error",
            "error": "HarborNotFoundError",
            "error_type": "HarborNotFoundError",
        }
    ]


@pytest.mark.asyncio
async def test_chat_stream_persists_the_partial_answer_once_when_abandoned_at_the_error() -> None:
    """The provider-error branch must not persist the same answer twice.

    It finishes the turn and then yields the terminal error frame. A client
    that goes away at exactly that yield throws ``GeneratorExit`` into the same
    ``try``, whose handler finishes the turn again -- appending the delivered
    text a second time, so history replays an answer the model produced once.
    """

    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    await memory.create(identity, kind="chat")
    service = _chat_service(_Runtime(_AdapterLikeChat()), memory)

    stream = service.stream(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1"),
    )
    events = [await anext(stream) for _ in range(3)]
    await stream.aclose()

    assert events[-1]["kind"] == "error"
    stored = await memory.recent_messages(identity, limit=10)
    assert [(message.role, message.content) for message in stored] == [
        ("user", "Hello"),
        ("assistant", "Hel"),
    ]


@pytest.mark.asyncio
async def test_app_service_forwards_the_project_check_to_the_chat_service() -> None:
    """The transport calls this before opening a stream; it must not be a no-op.

    This composition wires no control plane, so no project can be confirmed
    and every ``project_id`` is unknown -- which is exactly what proves the
    call reaches ``require_project`` rather than returning quietly.
    """

    service = _service(_Runtime(_ChatFacade()))

    await service.validate_chat_project(None, tenant_id="ACME")
    with pytest.raises(HarborNotFoundError, match="Project was not found"):
        await service.validate_chat_project("proj-1", tenant_id="ACME")
