"""Application-service tests for runtime-backed chat completions."""

from __future__ import annotations

import pytest
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.client import AppService, AppServiceFactories
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatUsage,
)
from harborrag_runtime.chat import ChatPrompt


class _ChatFacade:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.request: HarborChatRequest | None = None

    async def complete(
        self,
        request: HarborChatRequest,
        *,
        prompt: ChatPrompt | None = None,
    ) -> HarborChatResponse:
        del prompt
        if self.failure is not None:
            raise self.failure
        self.request = request
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


class _Runtime:
    def __init__(self, chat: _ChatFacade) -> None:
        self.chat = chat

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_chat_completion_attaches_access_metadata_and_projects_response() -> None:
    chat = _ChatFacade()
    runtime = _Runtime(chat)
    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
    )

    response = await service.chat_completion(
        HarborChatRequest(messages=(HarborChatMessage.user("Hello"),)),
        tenant_id="ACME",
        principal_id="reader-1",
    )

    assert response.ok is True
    assert chat.request is not None
    assert chat.request.metadata.tenant_id == "ACME"
    assert chat.request.metadata.user_id == "reader-1"
    assert response.data["message"] == {"role": "assistant", "content": "Hello"}
    assert response.data["usage"]["total_tokens"] == 3
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

    response = await service.chat_completion(
        HarborChatRequest(messages=(HarborChatMessage.user("Hello"),)),
        tenant_id="DEFAULT",
        principal_id="reader-1",
    )

    assert response.ok is False
    assert response.error == "RuntimeError"
    assert "secret provider response" not in str(response.data)
