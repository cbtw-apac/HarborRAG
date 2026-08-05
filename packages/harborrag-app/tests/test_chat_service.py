"""Application-service tests for runtime-backed, retrieval-grounded chat completions."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.client import AppService, AppServiceFactories
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
    HarborChatUsage,
)
from harborrag_core.models.chat.enums import StreamEventType
from harborrag_runtime.contracts import RetrievalResponse
from harborrag_runtime.sdk import RetrievalLane


class _ChatFacade:
    def __init__(
        self,
        failure: Exception | None = None,
        *,
        stream_failure: Exception | None = None,
    ) -> None:
        self.failure = failure
        self.stream_failure = stream_failure
        self.request: HarborChatRequest | None = None
        self.requests: list[HarborChatRequest] = []

    async def complete(
        self,
        request: HarborChatRequest,
        *,
        prompt: object | None = None,
    ) -> HarborChatResponse:
        del prompt
        if self.failure is not None:
            raise self.failure
        self.request = request
        self.requests.append(request)
        return HarborChatResponse(
            id="chat-1",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            message=HarborChatMessage.assistant("Hello"),
            finish_reason="stop",
            usage=HarborChatUsage(
                prompt_tokens=2,
                completion_tokens=1,
                total_tokens=3,
            ),
            provider_metadata={"private": "must not cross the API boundary"},
        )

    def stream(
        self,
        request: HarborChatRequest,
        *,
        prompt: object | None = None,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        del prompt
        self.request = request
        self.requests.append(request)
        return self._events()

    async def _events(self) -> AsyncIterator[HarborChatStreamChunk]:
        yield HarborChatStreamChunk(
            event=StreamEventType.TEXT_DELTA,
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            text_delta="Hello",
        )
        if self.stream_failure is not None:
            raise self.stream_failure
        yield HarborChatStreamChunk(
            event=StreamEventType.COMPLETED,
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            finish_reason="stop",
            usage=HarborChatUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        )


class _RetrievalFacade:
    def __init__(self, results: tuple[RetrievalResult, ...] = ()) -> None:
        self.results = results
        self.request = None

    async def search(self, request: object) -> RetrievalResponse:
        self.request = request
        return RetrievalResponse(
            request_id="retrieval-1",
            lane=RetrievalLane.HYBRID,
            results=self.results,
            diagnostics={},
        )


class _Runtime:
    def __init__(self, chat: _ChatFacade, retrieval: _RetrievalFacade | None = None) -> None:
        self.chat = chat
        self.retrieval = retrieval or _RetrievalFacade()

    async def aclose(self) -> None:
        return None


async def _options(
    service: AppService,
    *,
    tenant_id: str = "ACME",
    principal_id: str = "reader-1",
) -> ChatExecutionOptions:
    created = await service.create_chat_session(
        tenant_id=tenant_id,
        principal_id=principal_id,
    )
    return ChatExecutionOptions(session_id=str(created.data["session_id"]))


@pytest.mark.asyncio
async def test_chat_completion_attaches_access_metadata_and_projects_response() -> None:
    chat = _ChatFacade()
    results = (
        RetrievalResult(
            id="chunk-1",
            text="HarborRAG is a retrieval-augmented generation platform.",
            score=0.9,
            metadata={"document_id": "doc-1"},
        ),
    )
    runtime = _Runtime(chat, _RetrievalFacade(results))
    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
    )
    options = await _options(service)

    response = await service.chat_completion(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=options,
    )

    assert response.ok is True
    assert chat.request is not None
    assert chat.request.metadata.tenant_id == "ACME"
    assert chat.request.metadata.user_id is None
    assert chat.request.metadata.retrieval_query == "Hello"
    assert chat.request.metadata.chunk_ids == ("chunk-1",)
    assert len(chat.request.messages) == 1
    assert "HarborRAG is a retrieval-augmented generation platform." in (
        chat.request.messages[0].content
    )
    assert chat.request.messages[0].content.endswith("Question: Hello")
    assert response.data["message"] == {"role": "assistant", "content": "Hello"}
    assert response.data["usage"]["total_tokens"] == 3
    assert response.data["citations"] == (
        {"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9},
    )
    assert "deployment" not in response.data
    assert "provider_metadata" not in response.data


@pytest.mark.asyncio
async def test_chat_completion_hides_provider_failure_details() -> None:
    runtime = _Runtime(_ChatFacade(RuntimeError("secret provider response")))
    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
    )
    options = await _options(service, tenant_id="DEFAULT")

    response = await service.chat_completion(
        "Hello",
        tenant_id="DEFAULT",
        principal_id="reader-1",
        options=options,
    )

    assert response.ok is False
    assert response.error == "RuntimeError"
    assert "secret provider response" not in str(response.data)


@pytest.mark.asyncio
async def test_chat_completion_defaults_to_vector_only_retrieval() -> None:
    retrieval = _RetrievalFacade()
    runtime = _Runtime(_ChatFacade(), retrieval)
    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
    )
    options = await _options(service)

    await service.chat_completion(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=options,
    )

    assert retrieval.request is not None
    assert retrieval.request.observe_graph is False


@pytest.mark.asyncio
async def test_chat_completion_allows_per_request_graph_search_override() -> None:
    retrieval = _RetrievalFacade()
    runtime = _Runtime(_ChatFacade(), retrieval)
    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
    )
    options = await _options(service)

    await service.chat_completion(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id=options.session_id, graph_search=True),
    )

    assert retrieval.request.observe_graph is True


@pytest.mark.asyncio
async def test_chat_completion_recalls_only_two_latest_session_turns() -> None:
    chat = _ChatFacade()
    runtime = _Runtime(chat)
    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
    )
    options = await _options(service)

    await service.chat_completion(
        "First question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=options,
    )
    await service.chat_completion(
        "Second question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=options,
    )
    await service.chat_completion(
        "Third question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=options,
    )
    await service.chat_completion(
        "Fourth question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=options,
    )

    fourth = chat.requests[3]
    assert [message.content for message in fourth.messages] == [
        "Second question",
        "Hello",
        "Third question",
        "Hello",
        "Fourth question",
    ]
    assert fourth.metadata.user_id is None
    assert fourth.metadata.conversation_id == options.session_id
