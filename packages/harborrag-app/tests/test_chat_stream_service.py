"""Application-service tests for streamed retrieval-grounded chat."""

from __future__ import annotations

import pytest
from test_chat_service import _ChatFacade, _options, _RetrievalFacade, _Runtime
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.contracts import RetrievalResponse


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

    assert [event["kind"] for event in events] == ["citations", "chunk", "chunk"]
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

    assert [message.content for message in chat.requests[1].messages] == [
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

    assert events == [{"kind": "error", "error": "RuntimeError"}]
