"""Application-service tests for runtime-backed, retrieval-grounded chat completions."""

from __future__ import annotations

import pytest
from chat_service_fixtures import (
    FakeChatFacade,
    FakeMemoryFacade,
    FakeRetrievalFacade,
    FakeRuntime,
    replayed,
)
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.memory import MemoryPolicy


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
    # Cites the one retrieved source, so the projected citations are non-empty:
    # only sources an answer actually cites are reported.
    chat = FakeChatFacade(answer="Grounded in [Source 1].")
    results = (
        RetrievalResult(
            id="chunk-1",
            text="HarborRAG is a retrieval-augmented generation platform.",
            score=0.9,
            metadata={"document_id": "doc-1"},
        ),
    )
    runtime = FakeRuntime(chat, FakeRetrievalFacade(results))
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
    assert chat.request.metadata.user_id == "reader-1"
    assert chat.request.metadata.retrieval_query == "Hello"
    assert chat.request.metadata.chunk_ids == ("chunk-1",)
    assert len(chat.request.messages) == 1
    assert "HarborRAG is a retrieval-augmented generation platform." in (
        chat.request.messages[0].content
    )
    assert chat.request.messages[0].content.endswith("Question: Hello")
    assert response.data["message"] == {
        "role": "assistant",
        "content": "Grounded in [Source 1].",
    }
    assert response.data["usage"]["total_tokens"] == 3
    assert response.data["citations"] == (
        {"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9},
    )
    assert "deployment" not in response.data
    assert "provider_metadata" not in response.data


@pytest.mark.asyncio
async def test_chat_completion_hides_provider_failure_details() -> None:
    runtime = FakeRuntime(FakeChatFacade(RuntimeError("secret provider response")))
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
    retrieval = FakeRetrievalFacade()
    runtime = FakeRuntime(FakeChatFacade(), retrieval)
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
    retrieval = FakeRetrievalFacade()
    runtime = FakeRuntime(FakeChatFacade(), retrieval)
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
async def test_chat_completion_recalls_the_policy_window_of_session_turns() -> None:
    """The window is policy, not a fixed two turns: four messages keeps two turns."""

    chat = FakeChatFacade()
    runtime = FakeRuntime(
        chat,
        memory=FakeMemoryFacade(MemoryPolicy(recent_max_messages=4, summary_keep_messages=4)),
    )
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
    assert replayed(fourth) == [
        "Second question",
        "Hello",
        "Third question",
        "Hello",
        "Fourth question",
    ]
    assert fourth.metadata.user_id == "reader-1"
    assert fourth.metadata.conversation_id == options.session_id
