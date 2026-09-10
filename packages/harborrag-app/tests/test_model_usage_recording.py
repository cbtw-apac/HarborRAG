"""Chat and agent turns are attributable to a human and to a bill.

Chat used to be the one model family with no telemetry and no accounting: the
token counts went out to the caller and were then dropped, and no request
carried the end user at all. These tests pin one usage record per finished
turn, keyed by tenant and user, plus the guarantee that accounting never
fails a turn the provider was already paid for.
"""

from __future__ import annotations

import pytest
from chat_service_fixtures import (
    FakeChatFacade,
    FakeRetrievalFacade,
    FakeRuntime,
    FakeUsageRepository,
)

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

IDENTITY = ConversationIdentity("ACME", "svc-1", "session-1", "alice@example.com")
OPTIONS = ChatExecutionOptions(session_id="session-1", user_id="alice@example.com")


def _service(
    memory: InMemoryConversationMemory,
    *,
    usage: FakeUsageRepository | None = None,
    cost: float | None = None,
) -> ChatApplicationService:
    runtime = FakeRuntime(FakeChatFacade(cost=cost), FakeRetrievalFacade())
    return ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        RuntimeSettings(),
        memory=memory,
        usage=usage,  # type: ignore[arg-type]
    )


async def _session() -> InMemoryConversationMemory:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    return memory


async def _complete(service: ChatApplicationService) -> object:
    return await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="svc-1",
        options=OPTIONS,
    )


@pytest.mark.asyncio
async def test_a_completed_chat_turn_records_exactly_one_attributed_usage_row() -> None:
    memory = await _session()
    usage = FakeUsageRepository()

    response = await _complete(_service(memory, usage=usage, cost=0.0042))

    assert response.ok is True  # type: ignore[attr-defined]
    (record,) = usage.records
    assert (record.tenant_id, record.user_id, record.principal_id) == (
        "ACME",
        "alice@example.com",
        "svc-1",
    )
    assert (record.surface, record.session_id, record.run_id) == ("chat", "session-1", None)
    assert (record.logical_model, record.provider, record.provider_model) == (
        "primary",
        "mock",
        "mock-chat",
    )
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (2, 1, 3)
    assert record.estimated_cost_usd == 0.0042
    assert record.finish_reason == "stop"


@pytest.mark.asyncio
async def test_usage_totals_are_readable_per_tenant_and_per_user() -> None:
    memory = await _session()
    usage = FakeUsageRepository()
    service = _service(memory, usage=usage, cost=0.0042)

    await _complete(service)
    await _complete(service)

    totals = await usage.totals(tenant_id="ACME", user_id="alice@example.com")
    assert (totals.requests, totals.total_tokens) == (2, 6)
    assert await usage.totals(tenant_id="ACME", user_id="bob@example.com") == (
        type(totals)()  # nobody else spent anything in this tenant
    )


@pytest.mark.asyncio
async def test_a_usage_repository_failure_does_not_fail_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    memory = await _session()
    usage = FakeUsageRepository(RuntimeError("ledger unavailable"))

    response = await _complete(_service(memory, usage=usage))

    assert response.ok is True  # type: ignore[attr-defined]
    assert response.data["message"] == {"role": "assistant", "content": "Hello"}  # type: ignore[attr-defined]
    assert response.data["memory_persisted"] is True  # type: ignore[attr-defined]
    assert usage.records == []
    assert "Recording model usage failed" in caplog.text


@pytest.mark.asyncio
async def test_a_deployment_with_no_usage_ledger_still_answers() -> None:
    memory = await _session()

    response = await _complete(_service(memory))

    assert response.ok is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_conversation_is_owned_by_the_user_not_the_credential() -> None:
    """The same principal acting for a different person sees a different session."""

    memory = await _session()
    service = _service(memory)

    with pytest.raises(HarborNotFoundError):
        await service.complete(
            "Hello",
            tenant_id="ACME",
            principal_id="svc-1",
            options=ChatExecutionOptions(session_id="session-1", user_id="bob@example.com"),
        )
