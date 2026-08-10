"""Application-layer test doubles kept outside the production package."""

from __future__ import annotations

from collections.abc import Mapping

from app_test_agent import AgentServiceFixture
from app_test_chat import ChatServiceFixture
from app_test_graph_records import (
    graph_payload,
    projection_inventory_payload,
    retrieval_payload,
)
from app_test_ingestion import IngestionServiceFixture

from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.retrieval import GraphPathQuery, GraphSubgraphQuery, GraphTripletQuery
from harborrag_runtime.memory import new_session_id
from harborrag_runtime.sdk import RetrievalLane


class MockAppService(
    AgentServiceFixture,
    ChatServiceFixture,
    IngestionServiceFixture,
    BaseAppService,
):
    def __init__(self) -> None:
        self.submissions: list[IngestionCreateCommand] = []
        self.idempotency: dict[str, str] = {}
        self.retrieval_calls: list[dict[str, object]] = []
        self.graph_retrieval_calls: list[dict[str, object]] = []
        self.chat_calls: list[dict[str, object]] = []
        self.agent_calls: list[dict[str, object]] = []
        self.agent_resume_calls: list[dict[str, object]] = []
        self.conversation_sessions: set[tuple[str, str, str]] = set()

    async def create_chat_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return self._create_session(tenant_id, principal_id)

    async def create_agent_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return self._create_session(tenant_id, principal_id)

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool:
        return (tenant_id, principal_id, session_id) in self.conversation_sessions

    async def agent_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool:
        return (tenant_id, principal_id, session_id) in self.conversation_sessions

    def _create_session(self, tenant_id: str, principal_id: str) -> AppResponse:
        session_id = new_session_id()
        self.conversation_sessions.add((tenant_id, principal_id, session_id))
        return AppResponse(
            True,
            {"session_id": session_id, "greeting": "Hello! How can I help you today?"},
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
