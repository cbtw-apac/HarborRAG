"""Chat reads ``retrieval.top_k`` from the settings provider on every turn."""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.config_store import SettingsLevel
from harborrag_core.config_store.resolution import EffectiveSettings, EffectiveValue
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.project import Project
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

pytestmark = pytest.mark.unit


class FakeConfigProvider:
    """Resolves ``retrieval.top_k`` from overrides keyed by level label, project before tenant."""

    def __init__(self, overrides: dict[str, int] | None = None) -> None:
        self.overrides = overrides or {}
        self.unavailable = False

    async def effective(self, tenant_id: str, level: SettingsLevel) -> EffectiveSettings:
        del tenant_id
        if self.unavailable:
            raise HarborUnavailableError("settings store is unavailable")
        for candidate in reversed(level.chain()):
            if str(candidate) in self.overrides:
                value = EffectiveValue(self.overrides[str(candidate)], candidate.kind)
                return EffectiveSettings(1, {"retrieval.top_k": value})
        return EffectiveSettings(0, {"retrieval.top_k": EffectiveValue(5, "default")})


class FakeProjects:
    async def get(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> Project:
        return Project(id=project_id, name=project_id, collection="c", tenant_id="ACME")


async def _complete(
    provider: FakeConfigProvider, *, project_id: str | None, env_top_k: int = 5
) -> tuple[AppResponse, FakeRetrievalFacade]:
    memory = InMemoryConversationMemory()
    await memory.create(ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"))
    retrieval = FakeRetrievalFacade()
    runtime = FakeRuntime(FakeChatFacade(answer="Hello"), retrieval)
    service = ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        RuntimeSettings(chat_retrieval_top_k=env_top_k),
        memory=memory,
        projects=FakeProjects(),  # type: ignore[arg-type]
        config=provider,  # type: ignore[arg-type]
    )
    response = await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(
            session_id="session-1", user_id="reader-1", project_id=project_id
        ),
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

    response, retrieval = await _complete(provider, project_id=None)

    assert response.ok is False
    assert response.data["error_type"] == "HarborUnavailableError"
    assert retrieval.request is None
