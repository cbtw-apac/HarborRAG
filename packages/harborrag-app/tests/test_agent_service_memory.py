"""Agent application-service tests for per-message history and project scoping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from chat_service_fixtures import FakeUsageRepository
from test_agent_service import _Chat
from test_chat_service_project_memory import FakeProjects, _project

from harborrag_app.workflow_control.agent import AgentApplicationService, AgentExecutionOptions
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_runtime.agent import InMemoryAgentRunRepository
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")


def _service(
    memory: InMemoryConversationMemory,
    *,
    projects: FakeProjects | None = None,
    usage: FakeUsageRepository | None = None,
) -> AgentApplicationService:
    runtime = SimpleNamespace(chat=_Chat())
    return AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
        runs=InMemoryAgentRunRepository(),
        projects=projects,  # type: ignore[arg-type]
        usage=usage,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_agent_completion_writes_question_and_answer_tagged_with_the_run() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="agent")
    service = _service(memory)

    response = await service.complete(
        "first question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id="session-1"),
    )

    assert response.ok is True
    run_id = response.data["run_id"]
    assert response.data["project_id"] is None
    user, assistant = await memory.recent_messages(IDENTITY, limit=10)
    assert (user.role, user.content, user.run_id) == ("user", "first question", run_id)
    assert (assistant.role, assistant.content, assistant.run_id) == (
        "assistant",
        "answer-1",
        run_id,
    )
    assert assistant.token_count == 1
    assert assistant.tool_calls_json is None


@pytest.mark.asyncio
async def test_agent_completion_echoes_a_valid_project() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="agent")
    projects = FakeProjects(_project("proj-1"))
    service = _service(memory, projects=projects)

    response = await service.complete(
        "question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id="session-1", project_id="proj-1"),
    )

    assert response.ok is True
    assert response.data["project_id"] == "proj-1"
    assert projects.calls == [("proj-1", frozenset({"ACME"}))]


@pytest.mark.asyncio
async def test_agent_completion_rejects_an_unknown_project() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="agent")
    service = _service(memory, projects=FakeProjects(_project("foreign", tenant_id="OTHER")))

    with pytest.raises(HarborNotFoundError, match="Project was not found"):
        await service.complete(
            "question",
            tenant_id="ACME",
            principal_id="reader-1",
            options=AgentExecutionOptions(session_id="session-1", project_id="foreign"),
        )
    assert await memory.recent_messages(IDENTITY, limit=10) == ()


@pytest.mark.asyncio
async def test_agent_stream_reports_unknown_project_as_terminal_error() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="agent")
    service = _service(memory, projects=FakeProjects())

    events = [
        event
        async for event in service.stream(
            "question",
            tenant_id="ACME",
            principal_id="reader-1",
            options=AgentExecutionOptions(session_id="session-1", project_id="missing"),
        )
    ]

    assert events == [{"kind": "error", "error": "HarborNotFoundError"}]


@pytest.mark.asyncio
async def test_an_agent_run_records_one_usage_row_attributed_to_the_user() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(
        ConversationIdentity("ACME", "svc-1", "session-1", "alice@example.com"), kind="agent"
    )
    usage = FakeUsageRepository()
    service = _service(memory, usage=usage)

    response = await service.complete(
        "first question",
        tenant_id="ACME",
        principal_id="svc-1",
        options=AgentExecutionOptions(session_id="session-1", user_id="alice@example.com"),
    )

    assert response.ok is True
    (record,) = usage.records
    assert (record.tenant_id, record.user_id, record.principal_id) == (
        "ACME",
        "alice@example.com",
        "svc-1",
    )
    # A run spends across several turns, so the aggregate is what it cost.
    assert (record.surface, record.session_id, record.run_id) == (
        "agent",
        "session-1",
        response.data["run_id"],
    )
    assert record.completion_tokens == 1


@pytest.mark.asyncio
async def test_an_agent_usage_failure_does_not_fail_the_run() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="agent")
    usage = FakeUsageRepository(RuntimeError("ledger unavailable"))

    response = await _service(memory, usage=usage).complete(
        "first question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id="session-1"),
    )

    assert response.ok is True
    assert usage.records == []
