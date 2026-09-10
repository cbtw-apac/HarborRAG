"""Chat application-service tests for per-message history and project scoping."""

from __future__ import annotations

import json

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.domain.project import Project
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
RESULTS = (
    RetrievalResult(
        id="chunk-1",
        text="HarborRAG grounds answers.",
        score=0.9,
        metadata={"document_id": "doc-1"},
    ),
)


class FakeProjects:
    """``ProjectRepositoryPort.get`` double honouring the tenant filter."""

    def __init__(self, *projects: Project) -> None:
        self.projects = {project.id: project for project in projects}
        self.calls: list[tuple[str, frozenset[str] | None]] = []

    async def get(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> Project | None:
        self.calls.append((project_id, tenant_ids))
        project = self.projects.get(project_id)
        if project is None or (tenant_ids is not None and project.tenant_id not in tenant_ids):
            return None
        return project


def _project(project_id: str, tenant_id: str = "ACME") -> Project:
    return Project(
        id=project_id, name=project_id, collection=f"c-{project_id}", tenant_id=tenant_id
    )


def _service(
    memory: InMemoryConversationMemory,
    *,
    projects: FakeProjects | None = None,
    results: tuple[RetrievalResult, ...] = (),
) -> ChatApplicationService:
    runtime = FakeRuntime(FakeChatFacade(), FakeRetrievalFacade(results))
    return ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        RuntimeSettings(),
        memory=memory,
        projects=projects,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_chat_completion_writes_user_and_assistant_messages_with_citations() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    service = _service(memory, results=RESULTS)

    response = await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1"),
    )

    assert response.ok is True
    assert response.data["memory_persisted"] is True
    assert response.data["project_id"] is None
    user, assistant = await memory.recent_messages(IDENTITY, limit=10)
    assert (user.role, user.content, user.token_count, user.citations_json) == (
        "user",
        "Hello",
        None,
        None,
    )
    assert (assistant.role, assistant.content, assistant.token_count) == ("assistant", "Hello", 1)
    assert json.loads(assistant.citations_json or "null") == [
        {"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}
    ]
    assert user.message_id != assistant.message_id
    assert (user.run_id, assistant.run_id, assistant.tool_calls_json) == (None, None, None)
    # The turn view (phase-1 history reads) stays derivable from the message log.
    (turn,) = await memory.recent(IDENTITY, limit=2)
    assert (turn.user_content, turn.assistant_content) == ("Hello", "Hello")


@pytest.mark.asyncio
async def test_chat_stream_writes_messages_with_completion_tokens() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    service = _service(memory, results=RESULTS)

    events = [
        event
        async for event in service.stream(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1"),
        )
    ]

    assert events[0]["project_id"] is None
    assert [event["kind"] for event in events] == [
        "citations",
        "chunk",
        "chunk",
        "cited_sources",
    ]
    _, assistant = await memory.recent_messages(IDENTITY, limit=2)
    assert assistant.content == "Hello"
    assert assistant.token_count == 1
    assert assistant.citations_json is not None


@pytest.mark.asyncio
async def test_chat_completion_echoes_a_valid_project() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    projects = FakeProjects(_project("proj-1"))
    service = _service(memory, projects=projects)

    response = await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1", project_id="proj-1"),
    )

    assert response.ok is True
    assert response.data["project_id"] == "proj-1"
    assert projects.calls == [("proj-1", frozenset({"ACME"}))]


@pytest.mark.asyncio
@pytest.mark.parametrize("project_id", ["missing", "foreign"])
async def test_chat_completion_rejects_unknown_or_foreign_project(project_id: str) -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    service = _service(memory, projects=FakeProjects(_project("foreign", tenant_id="OTHER")))

    with pytest.raises(HarborNotFoundError, match="Project was not found"):
        await service.complete(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1", project_id=project_id),
        )
    assert await memory.recent_messages(IDENTITY, limit=10) == ()


@pytest.mark.asyncio
async def test_chat_completion_rejects_a_project_without_a_control_plane() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    service = _service(memory)

    with pytest.raises(HarborNotFoundError, match="Project was not found"):
        await service.complete(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1", project_id="proj-1"),
        )


@pytest.mark.asyncio
async def test_chat_stream_reports_unknown_project_as_terminal_error() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    service = _service(memory, projects=FakeProjects())

    events = [
        event
        async for event in service.stream(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1", project_id="missing"),
        )
    ]

    assert events == [
        {
            "kind": "error",
            "error": "HarborNotFoundError",
            "error_type": "HarborNotFoundError",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("project_id", ["missing", "foreign"])
async def test_validate_project_rejects_unknown_or_foreign_project(project_id: str) -> None:
    """What the streaming transport asks before it commits to a status line.

    Same rule as ``_require_scope`` applies inside a turn, asked early enough
    that the answer can still be a ``404``.
    """

    service = _service(
        InMemoryConversationMemory(),
        projects=FakeProjects(_project("foreign", tenant_id="OTHER")),
    )

    with pytest.raises(HarborNotFoundError, match="Project was not found"):
        await service.validate_project(project_id, tenant_id="ACME")


@pytest.mark.asyncio
async def test_validate_project_accepts_a_known_project_and_costs_nothing_without_one() -> None:
    projects = FakeProjects(_project("proj-1"))
    service = _service(InMemoryConversationMemory(), projects=projects)

    await service.validate_project("proj-1", tenant_id="ACME")
    await service.validate_project(None, tenant_id="ACME")

    # Only the real project was looked up: every turn without one would
    # otherwise pay for a control-plane read that can only answer "none".
    assert projects.calls == [("proj-1", frozenset({"ACME"}))]
