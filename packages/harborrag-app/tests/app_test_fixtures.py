"""Application-layer test doubles kept outside the production package."""

from __future__ import annotations

from collections.abc import Mapping

from app_test_chat import ChatServiceFixture
from app_test_ingestion import IngestionServiceFixture
from app_test_memory import FakeMemoryIndex, FakeMemoryStore
from app_test_retrieval_records import retrieval_payload

from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand
from harborrag_app.workflow_control.memory import (
    ConversationDirectoryService,
    MemoryAdminClientMixin,
    MemoryAdministrationService,
    conversation_summary_data,
)
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.ports.completion_requests import CompletionClaim
from harborrag_core.testing.control_plane_fakes import FakePendingEffectRepository
from harborrag_runtime.memory import (
    ConversationIdentity,
    InMemoryConversationMemory,
    new_session_id,
)
from harborrag_runtime.sdk import RetrievalLane, RetrievalMode


class MockAppService(
    ChatServiceFixture,
    IngestionServiceFixture,
    MemoryAdminClientMixin,
    BaseAppService,
):
    def __init__(self) -> None:
        self.submissions: list[IngestionCreateCommand] = []
        self.idempotency: dict[str, str] = {}
        self.direct_runs: list[dict[str, object]] = []
        self.retrieval_calls: list[dict[str, object]] = []
        self.chat_calls: list[dict[str, object]] = []
        # Model policy the double enforces, mirroring the runtime's rule: a
        # tenant listed in ``tenant_models`` is bounded by its own names, every
        # other tenant by the process-wide ``allowed_models``.
        self.allowed_models: set[str] = {"primary"}
        self.tenant_models: dict[str, set[str]] = {}
        self.known_projects: set[str] = {"proj-1"}
        self.tenant_projects: dict[str, set[str]] = {}
        # (tenant, principal, user, session_id): a conversation belongs to the end user.
        self.conversation_sessions: set[tuple[str, str, str, str]] = set()
        # Long-term memory administration runs against the real service over
        # in-memory doubles, so route tests exercise production visibility.
        self.memory_store = FakeMemoryStore()
        self.memory_index = FakeMemoryIndex()
        self.conversations = InMemoryConversationMemory()
        self._extraction = None
        self.pending_effects = FakePendingEffectRepository()
        self._memory_admin = MemoryAdministrationService(
            conversations=self.conversations,
            memories=self.memory_store,  # type: ignore[arg-type]
            index=self.memory_index,  # type: ignore[arg-type]
            pending_effects=self.pending_effects,
        )
        self._conversation_directory = ConversationDirectoryService(
            self.conversations, self._memory_admin
        )

    async def create_chat_session(  # noqa: PLR0913 - mirrors the application facade
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> AppResponse:
        await self.validate_chat_project(project_id, tenant_id=tenant_id)
        return await self._create_session(tenant_id, principal_id, user_id, title, project_id)

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
    ) -> bool:
        return await self.conversations.exists(
            ConversationIdentity(tenant_id, principal_id, session_id, user_id or DEFAULT_USER)
        )

    async def _create_session(  # noqa: PLR0913 - one component of the new session per argument
        self,
        tenant_id: str,
        principal_id: str,
        user_id: str | None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> AppResponse:
        """Write through to the conversation store the directory routes read.

        The fake keeps its own key set for the completion paths, but a created
        session must also become a listable conversation, or the session and
        directory surfaces would disagree in a way production cannot.
        """

        session_id = new_session_id()
        self.conversation_sessions.add(
            (tenant_id, principal_id, user_id or DEFAULT_USER, session_id)
        )
        owner = ConversationIdentity(tenant_id, principal_id, session_id, user_id or DEFAULT_USER)
        await self.conversations.create(owner, title=title, project_id=project_id)
        created = await self.conversations.get_conversation(owner)
        assert created is not None
        return AppResponse(True, conversation_summary_data(created))

    async def claim_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim:
        return await self.conversations.claim_completion(
            tenant_id=tenant_id,
            user_id=user_id,
            key=key,
            request_hash=request_hash,
        )

    async def finish_completion(  # noqa: PLR0913 - mirrors CompletionRequestStore
        self,
        *,
        tenant_id: str,
        user_id: str,
        key: str,
        request_hash: str,
        response_json: str | None,
        session_id: str | None = None,
    ) -> None:
        await self.conversations.finish_completion(
            tenant_id=tenant_id,
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            response_json=response_json,
            session_id=session_id,
        )

    async def release_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> None:
        await self.conversations.release_completion(
            tenant_id=tenant_id,
            user_id=user_id,
            key=key,
            request_hash=request_hash,
        )

    def health(self) -> AppResponse:
        return AppResponse(
            True,
            {
                "diagnostics": {
                    "mode": "development",
                    "runtime": {"provider": "app_test_double", "ready": True},
                }
            },
        )

    async def retrieve(  # noqa: PLR0913 - mirrors the application facade
        self,
        query: str,
        *,
        tenant_id: str | None = None,
        principal_id: str = "harborrag-cli",
        top_k: int = 10,
        filters: Mapping[str, object] | None = None,
        lane: RetrievalLane = RetrievalLane.HYBRID,
        mode: RetrievalMode = RetrievalMode.FLAT,
        observe_graph: bool = False,
        include_content: bool = False,
        include_metadata: bool = False,
        score_threshold: float = 0.0,
    ) -> AppResponse:
        self.retrieval_calls.append(
            {
                "query": query,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "top_k": top_k,
                "filters": dict(filters or {}),
                "lane": lane,
                "mode": mode,
                "observe_graph": observe_graph,
                "include_content": include_content,
                "include_metadata": include_metadata,
                "score_threshold": score_threshold,
            }
        )
        return AppResponse(
            True,
            retrieval_payload(
                lane=lane,
                top_k=top_k,
                include_content=include_content,
                include_metadata=include_metadata,
                score_threshold=score_threshold,
            ),
        )
