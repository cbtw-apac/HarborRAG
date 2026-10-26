"""Titles appear after persisted exchanges and never overwrite a manual edit."""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

IDENTITY = ConversationIdentity("ACME", "reader", "session-title", DEFAULT_USER)
OPTIONS = ChatExecutionOptions(session_id=IDENTITY.session_id)


async def setup(chat: FakeChatFacade):
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY)
    runtime = FakeRuntime(chat, FakeRetrievalFacade())
    service = ChatApplicationService(lambda: runtime, RuntimeSettings(), memory=memory)
    return service, memory


@pytest.mark.asyncio
async def test_title_is_generated_after_first_exchange_and_stays_stable():
    chat = FakeChatFacade()
    service, memory = await setup(chat)
    assert await memory.get_title(IDENTITY) is None
    first = await service.complete(
        "  Explain   the release policy  ",
        tenant_id="ACME",
        principal_id="reader",
        options=OPTIONS,
    )
    second = await service.complete(
        "A completely different question",
        tenant_id="ACME",
        principal_id="reader",
        options=OPTIONS,
    )
    assert first.ok and second.ok
    assert first.data["title"] == second.data["title"] == "Explain the release policy"
    assert len(chat.requests) == 2  # Title generation makes no additional model call.


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", ["Manual title", ""])
async def test_manual_rename_or_clear_wins_before_generation(manual):
    service, memory = await setup(FakeChatFacade())
    await memory.rename_conversation(IDENTITY, title=manual)
    result = await service.complete(
        "First question", tenant_id="ACME", principal_id="reader", options=OPTIONS
    )
    assert result.data["title"] == (manual or None)


@pytest.mark.asyncio
async def test_failed_generation_leaves_the_conversation_unnamed():
    service, memory = await setup(FakeChatFacade(failure=RuntimeError("unavailable")))
    result = await service.complete(
        "First question", tenant_id="ACME", principal_id="reader", options=OPTIONS
    )
    assert not result.ok
    assert await memory.get_title(IDENTITY) is None


@pytest.mark.asyncio
async def test_stream_final_result_contains_the_generated_title():
    service, memory = await setup(FakeChatFacade())
    events = [
        event
        async for event in service.stream(
            "Explain the release policy",
            tenant_id="ACME",
            principal_id="reader",
            options=OPTIONS,
        )
    ]
    assert events[-1]["kind"] == "result"
    assert (
        events[-1]["result"]["title"]
        == await memory.get_title(IDENTITY)
        == "Explain the release policy"
    )
