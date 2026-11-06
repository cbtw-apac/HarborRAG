"""The chosen model reaches the model request.

The route tests pin the transport contract; these pin the last hop, where the
name has to land on ``HarborChatRequest.logical_model`` or the provider would
silently answer from the catalog default instead.
"""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime
from workflow_control_fixtures import FakeComposition

from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService


async def _service_and_options(
    chat: FakeChatFacade,
    *,
    model: str | None,
) -> tuple[AppService, ChatExecutionOptions]:
    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: FakeRuntime(chat, FakeRetrievalFacade()),  # type: ignore[arg-type]
        ),
    )
    created = await service.create_chat_session(tenant_id="ACME", principal_id="reader-1")
    return service, ChatExecutionOptions(
        session_id=str(created.data["session_id"]),
        model=model,
    )


@pytest.mark.asyncio
async def test_the_chosen_model_lands_on_the_chat_request() -> None:
    chat = FakeChatFacade()
    service, options = await _service_and_options(chat, model="tenant-fast")

    response = await service.chat_completion(
        "Hello", tenant_id="ACME", principal_id="reader-1", options=options
    )

    assert response.ok
    assert chat.request is not None
    assert chat.request.logical_model == "tenant-fast"


@pytest.mark.asyncio
async def test_omitting_the_model_leaves_the_request_unchanged() -> None:
    chat = FakeChatFacade()
    service, options = await _service_and_options(chat, model=None)

    await service.chat_completion(
        "Hello", tenant_id="ACME", principal_id="reader-1", options=options
    )

    assert chat.request is not None
    # Unset, so the client resolves its configured default exactly as before.
    assert chat.request.logical_model is None


@pytest.mark.asyncio
async def test_a_streamed_turn_carries_the_chosen_model_too() -> None:
    chat = FakeChatFacade()
    service, options = await _service_and_options(chat, model="tenant-fast")

    async for _event in service.chat_stream(
        "Hello", tenant_id="ACME", principal_id="reader-1", options=options
    ):
        pass

    assert chat.request is not None
    assert chat.request.logical_model == "tenant-fast"
