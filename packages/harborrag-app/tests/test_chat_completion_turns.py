"""Story 5 AC10b and AC11 at the chat service: a busy session and the session project's settings."""

from __future__ import annotations

import asyncio

import pytest
from app_test_chat import ANSWER, PASSAGES, FakeConnectionCatalog, ProjectSets
from chat_service_fixtures import (
    FakeChatFacade,
    FakeConfigProvider,
    FakeRetrievalFacade,
    FakeRuntime,
    FakeUsageRepository,
)

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_app.workflow_control.chat.errors import ChatModelUnavailableError
from harborrag_app.workflow_control.memory import SessionLocks
from harborrag_app.workflow_control.memory.locks import ConversationTurnBusyError
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.models.errors import HarborChatConnectionError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

pytestmark = pytest.mark.unit

TENANT = "ACME"
READER = "reader-1"


class _Projects:
    known_projects = {"cpm"}
    tenant_projects: dict[str, set[str]] = {}


async def _service(
    chat: FakeChatFacade,
    provider: FakeConfigProvider | None = None,
    *,
    memory: InMemoryConversationMemory | None = None,
    usage: FakeUsageRepository | None = None,
) -> tuple[ChatApplicationService, InMemoryConversationMemory, FakeRetrievalFacade]:
    """One worker's chat service; pass ``memory`` to share a store with another worker."""
    if memory is None:
        memory = InMemoryConversationMemory()
        for session_id in ("ses_a", "ses_b"):
            await memory.create(
                ConversationIdentity(TENANT, READER, session_id, READER), project_id="cpm"
            )
    catalog = FakeConnectionCatalog()
    catalog.add("cpm", "cpm/cpm-tickets", ("scope-cpm-tickets",), documents=4)
    retrieval = FakeRetrievalFacade(PASSAGES)
    runtime = FakeRuntime(chat, retrieval)
    kwargs: dict[str, object] = {
        "memory": memory,
        "locks": SessionLocks(memory),
        "projects": ProjectSets(_Projects()),  # type: ignore[arg-type]
        "config": provider or FakeConfigProvider(),
        "usage": usage,
    }
    service = ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type, return-value]
        RuntimeSettings(),
        catalog=catalog,
        **kwargs,  # type: ignore[arg-type]
    )
    return service, memory, retrieval


def _ask(service: ChatApplicationService, session_id: str):  # type: ignore[no-untyped-def]
    return service.complete(
        "Who has Go experience?",
        tenant_id=TENANT,
        principal_id=READER,
        options=ChatExecutionOptions(session_id=session_id, user_id=READER),
    )


@pytest.mark.asyncio
async def test_a_second_turn_in_a_busy_session_is_refused_at_once() -> None:
    hold = asyncio.Event()
    chat = FakeChatFacade(answer=ANSWER, hold=hold)
    service, _, _ = await _service(chat)

    first = asyncio.create_task(_ask(service, "ses_a"))
    await asyncio.wait_for(chat.answering.wait(), timeout=2)
    try:
        with pytest.raises(HarborConflictError) as refused:
            await asyncio.wait_for(_ask(service, "ses_a"), timeout=2)
    finally:
        hold.set()
        await first

    assert refused.value.error_code == "session_busy"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_another_session_answers_while_one_is_busy() -> None:
    hold = asyncio.Event()
    chat = FakeChatFacade(answer=ANSWER, hold=hold)
    service, _, _ = await _service(chat)

    first = asyncio.create_task(_ask(service, "ses_a"))
    await asyncio.wait_for(chat.answering.wait(), timeout=2)
    second = asyncio.create_task(_ask(service, "ses_b"))
    await asyncio.sleep(0.05)
    hold.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert all(result.ok for result in results)
    assert len(chat.requests) == 2


@pytest.mark.asyncio
async def test_a_session_turn_reads_top_k_at_the_sessions_project_level() -> None:
    provider = FakeConfigProvider({"project:cpm": 2, "tenant": 7})
    service, _, retrieval = await _service(FakeChatFacade(answer=ANSWER), provider)

    await _ask(service, "ses_a")
    first = retrieval.request.top_k  # type: ignore[union-attr]
    provider.overrides = {"project:cpm": 3, "tenant": 7}
    await _ask(service, "ses_a")
    second = retrieval.request.top_k  # type: ignore[union-attr]

    assert (first, second) == (2, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["", "  ", "[9]"])
async def test_an_empty_answer_is_an_unavailable_model_and_stores_nothing(answer: str) -> None:
    service, memory, _ = await _service(FakeChatFacade(answer=answer))

    with pytest.raises(ChatModelUnavailableError):
        await _ask(service, "ses_a")

    owner = ConversationIdentity(TENANT, READER, "ses_a", READER)
    assert await memory.recent_messages(owner, limit=10) == ()


@pytest.mark.asyncio
async def test_another_worker_is_refused_through_the_shared_lease() -> None:
    hold = asyncio.Event()
    chat = FakeChatFacade(answer=ANSWER, hold=hold)
    first, memory, _ = await _service(chat)
    second, _, _ = await _service(FakeChatFacade(answer=ANSWER), memory=memory)

    turn = asyncio.create_task(_ask(first, "ses_a"))
    await asyncio.wait_for(chat.answering.wait(), timeout=2)
    try:
        with pytest.raises(ConversationTurnBusyError):
            await asyncio.wait_for(_ask(second, "ses_a"), timeout=2)
    finally:
        hold.set()
        await turn


@pytest.mark.asyncio
async def test_a_failed_turn_frees_the_session_for_the_next_one() -> None:
    chat = FakeChatFacade(HarborChatConnectionError("provider unreachable"), answer=ANSWER)
    service, memory, _ = await _service(chat)

    with pytest.raises(ChatModelUnavailableError):
        await _ask(service, "ses_a")
    chat.failure = None
    second = await _ask(service, "ses_a")

    assert second.ok
    owner = ConversationIdentity(TENANT, READER, "ses_a", READER)
    assert [message.role for message in await memory.recent_messages(owner, limit=10)] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_an_empty_answer_still_records_what_its_model_calls_cost() -> None:
    usage = FakeUsageRepository()
    service, _, _ = await _service(FakeChatFacade(answer=""), usage=usage)

    with pytest.raises(ChatModelUnavailableError):
        await _ask(service, "ses_a")

    assert [record.finish_reason for record in usage.records] == ["query_scope_gate", "stop"]
