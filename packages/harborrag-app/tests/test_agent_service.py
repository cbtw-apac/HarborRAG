"""Application wiring tests for the runtime-backed agent use case."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from harborrag_app.workflow_control.agent import (
    AgentApplicationService,
    AgentExecutionOptions,
)
from harborrag_app.workflow_control.agent.service import _run_options, agent_timeout_seconds
from harborrag_core.contracts.errors import HarborConfigurationError, HarborNotFoundError
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
)
from harborrag_runtime.agent import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunStatus,
    InMemoryAgentRunRepository,
)
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory


class _Chat:
    def __init__(self) -> None:
        self.requests = []
        self.prompts = []

    async def complete(self, request, *, prompt):
        self.requests.append(request)
        self.prompts.append(prompt)
        return HarborChatResponse(
            id=f"response-{len(self.requests)}",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="private",
            message=HarborChatMessage.assistant(f"answer-{len(self.requests)}"),
            finish_reason="stop",
            usage=HarborChatUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        )


def test_agent_service_applies_server_owned_time_and_token_budgets() -> None:
    options = _run_options(
        "ACME",
        "reader-1",
        AgentExecutionOptions(session_id="session-1"),
    )

    # Default 120s HTTP deadline minus the engine's 30s synthesis window minus
    # a 5s margin: the graceful TIMEOUT stop must land before the HTTP deadline.
    assert options.synthesis_timeout_seconds == 30.0
    assert options.timeout_seconds == 120.0 - 30.0 - 5.0
    assert options.max_total_tokens == 120_000
    assert options.user_id == "DEFAULT_USER"


def test_agent_service_derives_run_timeout_from_the_transport_deadline() -> None:
    streamed = _run_options(
        "ACME",
        "reader-1",
        AgentExecutionOptions(session_id="session-1", deadline_seconds=600.0, token_budget=50_000),
    )

    assert streamed.timeout_seconds == 600.0 - 30.0 - 5.0
    assert streamed.max_total_tokens == 50_000
    assert agent_timeout_seconds(None) == agent_timeout_seconds(120.0)
    with pytest.raises(HarborConfigurationError, match="no run budget"):
        agent_timeout_seconds(35.0)


@pytest.mark.asyncio
async def test_agent_service_uses_created_session_and_recalls_it_on_follow_up() -> None:
    chat = _Chat()
    runtime = SimpleNamespace(chat=chat)
    memory = InMemoryConversationMemory()
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
        runs=InMemoryAgentRunRepository(),
    )
    session_id = "session-1"
    await memory.create(
        ConversationIdentity("ACME", "reader-1", session_id, "reader-1"), kind="agent"
    )

    first = await service.complete(
        "first question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(user_id="reader-1", session_id=session_id),
    )
    second = await service.complete(
        "follow-up question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(user_id="reader-1", session_id=session_id),
    )

    assert first.ok is True
    assert second.ok is True
    assert second.data["session_id"] == session_id
    assert str(first.data["run_id"]).startswith("run-")
    assert first.data["stop_reason"] == "final_answer"
    assert chat.prompts == [ChatPrompt.DEFAULT, ChatPrompt.DEFAULT]
    assert [message.content for message in chat.requests[1].messages[1:]] == [
        "first question",
        "answer-1",
        "follow-up question",
    ]
    assert chat.requests[1].metadata.user_id == "reader-1"
    assert chat.requests[1].metadata.conversation_id == session_id


@pytest.mark.asyncio
async def test_agent_service_resumes_a_running_checkpoint() -> None:
    chat = _Chat()
    runtime = SimpleNamespace(chat=chat)
    memory = InMemoryConversationMemory()
    runs = InMemoryAgentRunRepository()
    session_id = "session-1"
    tenant_id, principal_id = "ACME", "reader-1"
    await memory.create(
        ConversationIdentity(tenant_id, principal_id, session_id, principal_id), kind="agent"
    )

    identity = AgentRunIdentity(tenant_id, principal_id, session_id, "run-crashed", principal_id)
    now = datetime.now(UTC)
    await runs.create(
        AgentCheckpoint(
            identity=identity,
            status=AgentRunStatus.RUNNING,
            step=0,
            version=1,
            messages=(HarborChatMessage.user("first question"),),
            executions=(),
            usage=HarborChatUsage(),
            stop_reason=None,
            response=None,
            created_at=now,
            updated_at=now,
        )
    )
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
        runs=runs,
    )

    resumed = await service.resume(
        "run-crashed",
        tenant_id=tenant_id,
        principal_id=principal_id,
        options=AgentExecutionOptions(user_id="reader-1", session_id=session_id),
    )

    assert resumed.ok is True
    assert resumed.data["run_id"] == "run-crashed"
    assert resumed.data["message"]["content"] == "answer-1"


@pytest.mark.asyncio
async def test_explicit_model_selection_survives_a_failed_run_and_resume() -> None:
    class InterruptedChat(_Chat):
        async def complete(self, request, *, prompt):
            if not self.requests:
                self.requests.append(request)
                raise ConnectionError("temporary provider failure")
            return await super().complete(request, prompt=prompt)

    chat = InterruptedChat()
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    await memory.create(identity)
    runs = InMemoryAgentRunRepository()
    service = AgentApplicationService(
        lambda: SimpleNamespace(chat=chat),
        memory=memory,
        runs=runs,
    )
    failed = await service.complete(
        "question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(
            session_id="session-1", user_id="reader-1", model="selected-model"
        ),
    )
    assert failed.ok is False
    checkpoint = next(iter(runs._checkpoints.values()))
    assert checkpoint.logical_model == "selected-model"

    resumed = await service.resume(
        checkpoint.identity.run_id,
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id="session-1", user_id="reader-1"),
    )

    assert resumed.ok is True
    assert [request.logical_model for request in chat.requests] == [
        "selected-model",
        "selected-model",
    ]


@pytest.mark.asyncio
async def test_agent_service_resume_of_unknown_run_raises_not_found() -> None:
    chat = _Chat()
    runtime = SimpleNamespace(chat=chat)
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=InMemoryConversationMemory(),
        runs=InMemoryAgentRunRepository(),
    )

    with pytest.raises(HarborNotFoundError):
        await service.resume(
            "missing-run",
            tenant_id="ACME",
            principal_id="reader-1",
            options=AgentExecutionOptions(user_id="reader-1", session_id="session-1"),
        )


@pytest.mark.asyncio
async def test_agent_service_resume_refuses_a_second_user_of_the_same_principal() -> None:
    """End-to-end shape of the ownership fix: the run is keyed by the human,
    so a second person behind the same service principal presenting the exact
    run id gets the not-found error the transport maps to 404 -- never the
    owner's conversation replayed into the model."""

    runtime = SimpleNamespace(chat=_Chat())
    memory = InMemoryConversationMemory()
    runs = InMemoryAgentRunRepository()
    session_id, tenant_id, principal_id = "session-1", "ACME", "shared-principal"
    await memory.create(
        ConversationIdentity(tenant_id, principal_id, session_id, "user-a"), kind="agent"
    )
    now = datetime.now(UTC)
    await runs.create(
        AgentCheckpoint(
            identity=AgentRunIdentity(tenant_id, principal_id, session_id, "run-crashed", "user-a"),
            status=AgentRunStatus.RUNNING,
            step=0,
            version=1,
            messages=(HarborChatMessage.user("private question"),),
            executions=(),
            usage=HarborChatUsage(),
            stop_reason=None,
            response=None,
            created_at=now,
            updated_at=now,
        )
    )
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
        runs=runs,
    )

    with pytest.raises(HarborNotFoundError):
        await service.resume(
            "run-crashed",
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=AgentExecutionOptions(session_id=session_id, user_id="user-b"),
        )


@pytest.mark.asyncio
async def test_agent_service_stream_yields_events_then_result() -> None:
    chat = _Chat()
    runtime = SimpleNamespace(chat=chat)
    memory = InMemoryConversationMemory()
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
        runs=InMemoryAgentRunRepository(),
    )
    session_id = "session-1"
    await memory.create(
        ConversationIdentity("ACME", "reader-1", session_id, "reader-1"), kind="agent"
    )

    items = [
        item
        async for item in service.stream(
            "question",
            tenant_id="ACME",
            principal_id="reader-1",
            options=AgentExecutionOptions(user_id="reader-1", session_id=session_id),
        )
    ]

    kinds = [item["kind"] for item in items]
    assert kinds[:-1] == ["event"] * (len(items) - 1)
    assert kinds[-1] == "result"
    assert items[0]["event"]["name"] == "run.started"
    assert items[-1]["result"]["message"]["content"] == "answer-1"


@pytest.mark.asyncio
async def test_agent_stream_error_carries_the_type_the_transport_branches_on() -> None:
    """Without ``error_type`` every agent failure reads as one generic outage.

    The transport's error table is keyed on it, so a missing key collapses a
    busy turn and a lost lease into "Chat service is unavailable" and tells the
    caller to back off when it should retry. The rag surface is pinned by
    ``test_chat_stream_error_frame_names_a_scope_failure``; this is its agent twin.
    """

    runtime = SimpleNamespace(chat=_Chat())
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=InMemoryConversationMemory(),
        runs=InMemoryAgentRunRepository(),
    )

    items = [
        item
        async for item in service.stream(
            "question",
            tenant_id="ACME",
            principal_id="reader-1",
            options=AgentExecutionOptions(user_id="reader-1", session_id="missing-session"),
        )
    ]

    assert [item["kind"] for item in items] == ["error"]
    assert items[0]["error_type"] == "HarborNotFoundError"


@pytest.mark.asyncio
async def test_agent_service_stream_cancels_the_background_run_on_early_close() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    class _BlockingChat:
        async def complete(self, request, *, prompt):
            del request, prompt
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("release must never be set in this test")

    runtime = SimpleNamespace(chat=_BlockingChat())
    memory = InMemoryConversationMemory()
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
        runs=InMemoryAgentRunRepository(),
    )
    session_id = "session-1"
    await memory.create(
        ConversationIdentity("ACME", "reader-1", session_id, "reader-1"), kind="agent"
    )

    stream = service.stream(
        "question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(user_id="reader-1", session_id=session_id),
    )

    first = await stream.__anext__()
    assert first["kind"] == "event"
    assert first["event"]["name"] == "run.started"
    await asyncio.wait_for(started.wait(), timeout=1)

    await stream.aclose()

    await asyncio.wait_for(cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_agent_service_accepts_a_chat_conversation_for_either_delivery_mode() -> None:
    """The stored creation kind does not prevent switching execution mode."""

    chat = _Chat()
    runtime = SimpleNamespace(chat=chat)
    memory = InMemoryConversationMemory()
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
        runs=InMemoryAgentRunRepository(),
    )
    await memory.create(
        ConversationIdentity("ACME", "reader-1", "chat-session", "reader-1"), kind="chat"
    )

    result = await service.complete(
        "question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(user_id="reader-1", session_id="chat-session"),
    )
    events = [
        item
        async for item in service.stream(
            "question",
            tenant_id="ACME",
            principal_id="reader-1",
            options=AgentExecutionOptions(user_id="reader-1", session_id="chat-session"),
        )
    ]
    assert result.ok is True
    assert events[-1]["kind"] == "result"
    assert len(chat.requests) == 2
