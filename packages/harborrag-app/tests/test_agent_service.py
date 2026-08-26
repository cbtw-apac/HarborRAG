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
from harborrag_app.workflow_control.agent.service import _run_options
from harborrag_core.contracts.errors import HarborNotFoundError
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

    assert options.timeout_seconds == 120.0
    assert options.max_total_tokens == 32_768


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
    await memory.create(ConversationIdentity("ACME", "reader-1", session_id))

    first = await service.complete(
        "first question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id=session_id),
    )
    second = await service.complete(
        "follow-up question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id=session_id),
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
    assert chat.requests[1].metadata.user_id is None
    assert chat.requests[1].metadata.conversation_id == session_id


@pytest.mark.asyncio
async def test_agent_service_resumes_a_running_checkpoint() -> None:
    chat = _Chat()
    runtime = SimpleNamespace(chat=chat)
    memory = InMemoryConversationMemory()
    runs = InMemoryAgentRunRepository()
    session_id = "session-1"
    tenant_id, principal_id = "ACME", "reader-1"
    await memory.create(ConversationIdentity(tenant_id, principal_id, session_id))

    identity = AgentRunIdentity(tenant_id, principal_id, session_id, "run-crashed")
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
        options=AgentExecutionOptions(session_id=session_id),
    )

    assert resumed.ok is True
    assert resumed.data["run_id"] == "run-crashed"
    assert resumed.data["message"]["content"] == "answer-1"


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
            options=AgentExecutionOptions(session_id="session-1"),
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
    await memory.create(ConversationIdentity("ACME", "reader-1", session_id))

    items = [
        item
        async for item in service.stream(
            "question",
            tenant_id="ACME",
            principal_id="reader-1",
            options=AgentExecutionOptions(session_id=session_id),
        )
    ]

    kinds = [item["kind"] for item in items]
    assert kinds[:-1] == ["event"] * (len(items) - 1)
    assert kinds[-1] == "result"
    assert items[0]["event"]["name"] == "run.started"
    assert items[-1]["result"]["message"]["content"] == "answer-1"


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
    await memory.create(ConversationIdentity("ACME", "reader-1", session_id))

    stream = service.stream(
        "question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id=session_id),
    )

    first = await stream.__anext__()
    assert first["kind"] == "event"
    assert first["event"]["name"] == "run.started"
    await asyncio.wait_for(started.wait(), timeout=1)

    await stream.aclose()

    await asyncio.wait_for(cancelled.wait(), timeout=1)
