"""A tenant that has not ingested gets an actionable answer, not a 503.

``prepare_turn`` runs inside ``complete``'s ``except Exception``, so anything
retrieval raises became "the chat service is unavailable". For a missing
index that is wrong twice over: the service is healthy, and the caller's fix
is to ingest rather than to retry.
"""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.contracts.errors import HarborNoIndexedContentError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")


class _NoIndexRetrieval:
    async def search(self, request: object) -> object:
        del request
        raise HarborNoIndexedContentError(
            "No content has been ingested yet, so there is nothing to search"
        )


async def _service() -> ChatApplicationService:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    runtime = FakeRuntime(FakeChatFacade(), _NoIndexRetrieval())  # type: ignore[arg-type]
    return ChatApplicationService(lambda: runtime, RuntimeSettings(), memory=memory)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_completion_propagates_no_indexed_content_instead_of_failing() -> None:
    """It must reach the transport as itself, so the transport can send 409."""

    service = await _service()

    with pytest.raises(HarborNoIndexedContentError):
        await service.complete(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1"),
        )


@pytest.mark.asyncio
async def test_stream_names_no_indexed_content_in_its_error_event() -> None:
    """A stream cannot change status, so the terminal frame has to say which."""

    service = await _service()

    events = [
        event
        async for event in service.stream(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1"),
        )
    ]

    assert events[-1]["kind"] == "error"
    assert events[-1]["error_type"] == "HarborNoIndexedContentError"
