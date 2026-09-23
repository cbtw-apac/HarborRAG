"""Application-layer test doubles kept outside the production package."""

from __future__ import annotations

from collections.abc import Mapping

from app_test_agent import AgentServiceFixture
from app_test_chat import ChatServiceFixture
from app_test_graph_records import (
    graph_conflict,
    graph_payload,
    projection_inventory_payload,
    retrieval_payload,
)
from app_test_ingestion import IngestionServiceFixture
from app_test_memory import FakeMemoryIndex, FakeMemoryStore

from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand
from harborrag_app.workflow_control.memory import (
    ConversationDirectoryService,
    MemoryAdminClientMixin,
    MemoryAdministrationService,
)
from harborrag_core.contracts.errors import HarborConflictError, HarborNotFoundError
from harborrag_core.domain.graph_conflict import ConflictAction, ConflictStatus, GraphConflict
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.ports.completion_requests import CompletionClaim
from harborrag_core.ports.conversation import ConversationKind
from harborrag_core.retrieval import GraphPathQuery, GraphSubgraphQuery, GraphTripletQuery
from harborrag_core.testing.control_plane_fakes import FakePendingEffectRepository
from harborrag_runtime.memory import (
    ConversationIdentity,
    InMemoryConversationMemory,
    new_session_id,
)
from harborrag_runtime.sdk import RetrievalLane, RetrievalMode


class MockAppService(
    AgentServiceFixture,
    ChatServiceFixture,
    IngestionServiceFixture,
    MemoryAdminClientMixin,
    BaseAppService,
):
    def __init__(self) -> None:
        self.submissions: list[IngestionCreateCommand] = []
        self.idempotency: dict[str, str] = {}
        self.task_list_calls: list[dict[str, object]] = []
        self.direct_runs: list[dict[str, object]] = []
        self.retrieval_calls: list[dict[str, object]] = []
        self.graph_retrieval_calls: list[dict[str, object]] = []
        self.chat_calls: list[dict[str, object]] = []
        # Model policy the double enforces, mirroring the runtime's rule: a
        # tenant listed in ``tenant_models`` is bounded by its own names, every
        # other tenant by the process-wide ``allowed_models``.
        self.allowed_models: set[str] = {"primary"}
        self.tenant_models: dict[str, set[str]] = {}
        self.known_projects: set[str] = {"proj-1"}
        self.agent_calls: list[dict[str, object]] = []
        self.agent_resume_calls: list[dict[str, object]] = []
        # (tenant, principal, user, session_id, kind): a conversation belongs
        # to the end user, and is bound to the surface that created it.
        self.conversation_sessions: set[tuple[str, str, str, str, str]] = set()
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
        default_conflict = graph_conflict()
        self.graph_conflicts: dict[str, GraphConflict] = {default_conflict.id: default_conflict}
        self.graph_conflict_resolve_calls: list[dict[str, object]] = []

    async def create_chat_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
        title: str | None = None,
        kind: ConversationKind = "chat",
    ) -> AppResponse:
        return await self._create_session(tenant_id, principal_id, user_id, kind, title)

    async def create_agent_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
    ) -> AppResponse:
        return await self._create_session(tenant_id, principal_id, user_id, "agent")

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

    async def agent_session_exists(
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

    @staticmethod
    def _key(  # noqa: PLR0913 - one component of the isolation key per argument
        tenant_id: str,
        principal_id: str,
        user_id: str | None,
        session_id: str,
        kind: str,
    ) -> tuple[str, str, str, str, str]:
        return (tenant_id, principal_id, user_id or DEFAULT_USER, session_id, kind)

    async def _create_session(  # noqa: PLR0913 - one component of the new session per argument
        self,
        tenant_id: str,
        principal_id: str,
        user_id: str | None,
        kind: str,
        title: str | None = None,
    ) -> AppResponse:
        """Write through to the conversation store the directory routes read.

        The fake keeps its own key set for the completion paths, but a created
        session must also become a listable conversation, or the session and
        directory surfaces would disagree in a way production cannot.
        """

        session_id = new_session_id()
        self.conversation_sessions.add(
            self._key(tenant_id, principal_id, user_id, session_id, kind)
        )
        await self.conversations.create(
            ConversationIdentity(tenant_id, principal_id, session_id, user_id or DEFAULT_USER),
            kind="agent" if kind == "agent" else "chat",
            title=title,
        )
        return AppResponse(
            True,
            {"session_id": session_id, "greeting": "Hello! How can I help you today?"},
        )

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

    async def retrieve_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return self._graph_response("triplets", query, tenant_id, principal_id)

    async def retrieve_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return self._graph_response("paths", query, tenant_id, principal_id)

    async def retrieve_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return self._graph_response("subgraph", query, tenant_id, principal_id)

    def _graph_response(
        self,
        operation: str,
        query: object,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        self.graph_retrieval_calls.append(
            {
                "operation": operation,
                "query": query,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
            }
        )
        return AppResponse(True, graph_payload(operation))

    async def projection_inventory(self, tenant: str) -> dict[str, object]:
        return projection_inventory_payload(tenant)

    async def delete_projections(
        self,
        tenant: str,
        *,
        confirmation: str,
        stores: frozenset[str],
    ) -> dict[str, object]:
        del confirmation
        return {
            "tenant": tenant,
            "deleted_stores": sorted(stores),
            "before": await self.projection_inventory(tenant),
            "reindex_required": True,
        }

    async def list_graph_conflicts(
        self,
        *,
        cursor: str | None,
        limit: int,
        tenant_ids: frozenset[str] | None,
        status: ConflictStatus | None = None,
    ) -> AppResponse:
        del cursor
        conflicts = [
            c
            for c in self.graph_conflicts.values()
            if (tenant_ids is None or c.tenant_id in tenant_ids)
            and (status is None or c.status == status)
        ][:limit]
        return AppResponse(True, {"conflicts": conflicts, "next_cursor": None})

    async def resolve_graph_conflict(
        self,
        conflict_id: str,
        *,
        action: ConflictAction,
        actor: str,
        tenant_ids: frozenset[str] | None,
    ) -> AppResponse:
        conflict = self.graph_conflicts.get(conflict_id)
        if conflict is None or (tenant_ids is not None and conflict.tenant_id not in tenant_ids):
            raise HarborNotFoundError(f"graph conflict {conflict_id!r} not found")
        if conflict.status == "resolved":
            raise HarborConflictError(f"graph conflict {conflict_id!r} is already resolved")
        self.graph_conflict_resolve_calls.append(
            {"conflict_id": conflict_id, "action": action, "actor": actor}
        )
        conflict.status = "resolved"
        conflict.action = action
        conflict.resolved_by = actor
        return AppResponse(True, {"conflict": conflict})

    async def list_projects(self, *, tenant_ids: frozenset[str] | None = None) -> AppResponse:
        del tenant_ids
        return AppResponse(True, {"projects": []})

    async def get_project(
        self, project_id: str, *, tenant_ids: frozenset[str] | None = None
    ) -> AppResponse:
        del project_id, tenant_ids
        return AppResponse(True, {"project": None})

    async def list_sources(
        self, project_id: str | None = None, *, tenant_ids: frozenset[str] | None = None
    ) -> AppResponse:
        del project_id, tenant_ids
        return AppResponse(True, {"sources": []})

    async def get_source(
        self, source_id: str, *, tenant_ids: frozenset[str] | None = None
    ) -> AppResponse:
        del source_id, tenant_ids
        return AppResponse(True, {"source": None})

    async def create_source(  # noqa: PLR0913 - mirrors the ports.py protocol signature
        self,
        *,
        tenant_id: str,
        project_id: str,
        source_type: str,
        name: str,
        config: Mapping[str, object],
        schedule: str | None,
        actor: str,
    ) -> AppResponse:
        del tenant_id, project_id, source_type, name, config, schedule, actor
        return AppResponse(True, {"source": None})

    async def update_source(
        self,
        source_id: str,
        *,
        updates: dict[str, object],
        actor: str,
        tenant_ids: frozenset[str] | None = None,
    ) -> AppResponse:
        del source_id, updates, actor, tenant_ids
        return AppResponse(True, {"source": None})

    async def delete_source(
        self, source_id: str, *, actor: str, tenant_ids: frozenset[str] | None = None
    ) -> AppResponse:
        del actor, tenant_ids
        return AppResponse(True, {"source_id": source_id})

    async def list_activity(
        self, limit: int = 50, *, tenant_ids: frozenset[str] | None = None
    ) -> AppResponse:
        del limit, tenant_ids
        return AppResponse(True, {"activity": []})

    async def get_settings(self) -> AppResponse:
        return AppResponse(True, {"settings": WorkspaceSettings(tenant_id="DEFAULT")})

    async def get_metrics(self, *, tenant_ids: frozenset[str] | None = None) -> AppResponse:
        del tenant_ids
        return AppResponse(
            True,
            {
                "projects_total": 0,
                "sources_total": 0,
                "documents_total": 0,
                "chunks_total": 0,
                "jobs_by_status": {
                    "queued": 0,
                    "running": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "cancelled": 0,
                },
            },
        )
