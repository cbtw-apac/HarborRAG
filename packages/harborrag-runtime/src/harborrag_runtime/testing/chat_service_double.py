"""Chat behaviour of the shared application-service test double.

Runs the real chat pipeline over a fake model, fake retrieval and the double's in-memory
conversation store. Test support only: nothing in production imports this module.
"""

from __future__ import annotations

from types import SimpleNamespace

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_engine.chat.scope import ConnectionSources
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory
from harborrag_runtime.application.chat import ChatApplicationService
from harborrag_runtime.application.memory import ConversationSessionService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.testing.chat_fakes import (
    FakeChatFacade,
    FakeConfigProvider,
    FakeRetrievalFacade,
    FakeRuntime,
)

PASSAGES = (
    RetrievalResult(
        id="chk_1",
        text="The deploy runbook lists the steps that promote a release to production.",
        score=0.82,
        metadata={
            "document_id": "doc_1",
            "document_title": "DEMO-1 Deploy runbook",
            "citation_locator": {"uri": "https://jira.example.test/browse/DEMO-1"},
        },
    ),
    RetrievalResult(
        id="chk_2",
        text="Roll the billing service back to the previous release tag, then check its health. "
        * 8,
        score=0.91,
        metadata={
            "document_id": "doc_2",
            "document_title": "DEMO-2 Rollback runbook",
            "citation_locator": {"page_start": 2, "page_end": 2},
        },
    ),
)
"""Two passages of project ``demo``; the second is longer than a snippet."""

ANSWER = "Two runbooks match: DEMO-2 [2] and DEMO-1 [1]; see also [9]. DEMO-2 [2] covers rollback."
"""Cites retrieval passage 2 first, then 1, then a passage that was never retrieved."""


class ProjectSets:
    """``ProjectRepositoryPort`` over the double's ``known_projects`` and ``tenant_projects``."""

    def __init__(self, owner: ChatServiceFixture) -> None:
        self._owner = owner

    async def get(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> object | None:
        tenants = tenant_ids or frozenset()
        if project_id in self._owner.known_projects or any(
            project_id in self._owner.tenant_projects.get(tenant, set()) for tenant in tenants
        ):
            return SimpleNamespace(id=project_id)
        return None


class FakeConnectionCatalog:
    """The connections of each project with the source scopes and documents they have indexed."""

    def __init__(self) -> None:
        self.projects: dict[str, list[ConnectionSources]] = {}

    def add(
        self, project_id: str, ref: str, source_scope_ids: tuple[str, ...], *, documents: int
    ) -> None:
        """Give ``project_id`` one more connection."""
        self.projects.setdefault(project_id, []).append(
            ConnectionSources(ref=ref, source_scope_ids=source_scope_ids, published=documents > 0)
        )

    async def project_connections(
        self, tenant_id: str, project_id: str
    ) -> tuple[ConnectionSources, ...]:
        del tenant_id
        return tuple(self.projects.get(project_id, ()))


class ChatServiceFixture:
    """Compose the production chat service over in-memory and fake collaborators."""

    conversations: InMemoryConversationMemory
    known_projects: set[str]
    tenant_projects: dict[str, set[str]]

    def _compose_chat(self) -> None:
        self.chat_runtime = FakeRuntime(
            FakeChatFacade(answer=ANSWER), FakeRetrievalFacade(PASSAGES)
        )
        self.settings_overrides = FakeConfigProvider()
        self.catalog = FakeConnectionCatalog()
        self.use_chat_settings(RuntimeSettings())

    def use_chat_settings(self, settings: RuntimeSettings) -> None:
        """Rebuild the chat service with ``settings``, keeping every store and fake."""
        self._sessions = ConversationSessionService(self.conversations)
        self._chat = ChatApplicationService(
            lambda: self.chat_runtime,  # type: ignore[arg-type, return-value]
            settings,
            memory=self.conversations,
            projects=ProjectSets(self),  # type: ignore[arg-type]
            config=self.settings_overrides,  # type: ignore[arg-type]
            catalog=self.catalog,
        )

    @property
    def chat(self) -> FakeChatFacade:
        """The fake model: ``requests`` are answer calls, ``gate_requests`` scope checks."""
        return self.chat_runtime.chat

    @property
    def retrieval(self) -> FakeRetrievalFacade:
        """The fake retrieval facade and the requests it received."""
        return self.chat_runtime.retrieval
