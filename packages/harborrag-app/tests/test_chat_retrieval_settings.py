"""Chat reads ``retrieval.top_k`` of the session's project from the settings provider on every turn."""

from __future__ import annotations

import pytest
from app_test_chat import FakeConnectionCatalog
from chat_service_fixtures import (
    FakeChatFacade,
    FakeConfigProvider,
    FakeRetrievalFacade,
    FakeRuntime,
)

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.project import Project
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

pytestmark = pytest.mark.unit


class FakeProjects:
    async def get(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> Project:
        return Project(id=project_id, name=project_id, collection="c", tenant_id="ACME")


async def _complete(
    provider: FakeConfigProvider, *, project_id: str | None, env_top_k: int = 5
) -> tuple[AppResponse, FakeRetrievalFacade]:
    memory = InMemoryConversationMemory()
    await memory.create(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), project_id=project_id
    )
    catalog = FakeConnectionCatalog()
    catalog.add("cpm", "cpm/docs", ("scope-docs",), documents=1)
    retrieval = FakeRetrievalFacade()
    runtime = FakeRuntime(FakeChatFacade(answer="Hello"), retrieval)
    service = ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        RuntimeSettings(chat_retrieval_top_k=env_top_k),
        memory=memory,
        projects=FakeProjects(),  # type: ignore[arg-type]
        config=provider,  # type: ignore[arg-type]
        catalog=catalog,
    )
    response = await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
    )
    return response, retrieval


async def _search_top_k(
    provider: FakeConfigProvider, *, project_id: str | None, env_top_k: int = 5
) -> int:
    _, retrieval = await _complete(provider, project_id=project_id, env_top_k=env_top_k)
    return retrieval.request.top_k  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_chat_uses_project_top_k() -> None:
    provider = FakeConfigProvider({"project:cpm": 7, "tenant": 3})

    assert await _search_top_k(provider, project_id="cpm") == 7


@pytest.mark.asyncio
async def test_chat_uses_tenant_top_k_without_a_project() -> None:
    assert await _search_top_k(FakeConfigProvider({"tenant": 3}), project_id=None) == 3


@pytest.mark.asyncio
async def test_chat_default_keeps_env_top_k() -> None:
    assert await _search_top_k(FakeConfigProvider(), project_id="cpm", env_top_k=9) == 9


@pytest.mark.asyncio
async def test_chat_without_a_provider_keeps_env_top_k() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"))
    retrieval = FakeRetrievalFacade()
    runtime = FakeRuntime(FakeChatFacade(answer="Hello"), retrieval)
    service = ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        RuntimeSettings(chat_retrieval_top_k=9),
        memory=memory,
    )

    await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
    )

    assert retrieval.request.top_k == 9  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_chat_surfaces_an_unavailable_provider() -> None:
    provider = FakeConfigProvider()
    provider.unavailable = True

    with pytest.raises(HarborUnavailableError):
        await _complete(provider, project_id=None)
