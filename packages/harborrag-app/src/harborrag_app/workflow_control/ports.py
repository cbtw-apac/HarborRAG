from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping

from harborrag_core.retrieval import (
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_runtime.sdk import RetrievalLane

from .agent import AgentExecutionOptions
from .chat.options import ChatExecutionOptions
from .schemas import AppResponse


class BaseAppService(ABC):
    """Application facade shared by the HTTP and CLI transports."""

    @abstractmethod
    def health(self) -> AppResponse:
        raise NotImplementedError

    @abstractmethod
    def ingest_once(self) -> AppResponse:
        raise NotImplementedError

    async def runtime_health(self) -> AppResponse:
        """Return live runtime health where the selected service supports it."""

        return self.health()

    async def recover_pending_submissions(self, *, limit: int = 100) -> int:
        """Recover durable workflow starts where the concrete service supports it."""

        del limit
        return 0

    async def create_chat_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        raise NotImplementedError

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool:
        raise NotImplementedError

    async def create_agent_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        raise NotImplementedError

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse:
        """Generate one retrieval-grounded chat completion."""

        raise NotImplementedError

    def chat_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]:
        """Stream one retrieval-grounded chat completion as ``{"kind": ...}`` events."""

        raise NotImplementedError

    async def agent_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        """Run one bounded multi-turn agent completion."""

        raise NotImplementedError

    async def start_ingestion(  # noqa: PLR0913 - legacy CLI port
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str | None = None,
        connection_id: str | None = None,
        source_scope_id: str | None = None,
        path: str | None = None,
        pattern: str | None = None,
        recursive: bool = True,
        updated_after: str | None = None,
        max_artifacts: int | None = None,
        include_attachments: bool = True,
        filters: Mapping[str, object] | None = None,
        force_reprocess: bool = False,
        batch_size: int | None = None,
        document_concurrency: int | None = None,
        wait: bool = False,
    ) -> AppResponse:
        raise NotImplementedError

    async def ingestion_status(self, run_id: str) -> AppResponse:
        raise NotImplementedError

    async def ingestion_result(self, run_id: str) -> AppResponse:
        raise NotImplementedError

    async def retrieve(  # noqa: PLR0913 - explicit retrieval policy is transport-neutral
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
        raise NotImplementedError

    async def retrieve_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        raise NotImplementedError

    async def retrieve_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        raise NotImplementedError

    async def retrieve_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        raise NotImplementedError

    async def control_ingestion(
        self,
        run_id: str,
        action: str,
    ) -> AppResponse:
        raise NotImplementedError

    async def projection_inventory(self, tenant: str) -> dict[str, object]:
        raise NotImplementedError

    async def delete_projections(
        self,
        tenant: str,
        *,
        confirmation: str,
        stores: frozenset[str],
    ) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    async def list_projects(self, *, tenant_ids: frozenset[str] | None) -> AppResponse:
        """Projects within ``tenant_ids`` (ML1 read side); data={"projects": list[Project]}."""
        raise NotImplementedError

    @abstractmethod
    async def get_project(
        self, project_id: str, *, tenant_ids: frozenset[str] | None
    ) -> AppResponse:
        """One project by id within ``tenant_ids``; raises HarborNotFoundError when missing."""
        raise NotImplementedError

    @abstractmethod
    async def list_sources(
        self, project_id: str | None = None, *, tenant_ids: frozenset[str] | None
    ) -> AppResponse:
        """Sources within ``tenant_ids``, optionally scoped to a project; data={"sources": [...]}."""
        raise NotImplementedError

    @abstractmethod
    async def get_source(self, source_id: str, *, tenant_ids: frozenset[str] | None) -> AppResponse:
        """One source by id within ``tenant_ids``; raises HarborNotFoundError when missing."""
        raise NotImplementedError

    @abstractmethod
    async def list_activity(
        self, limit: int = 50, *, tenant_ids: frozenset[str] | None
    ) -> AppResponse:
        """Most recent audit entries within ``tenant_ids``; data={"activity": [...]}."""
        raise NotImplementedError

    @abstractmethod
    async def get_settings(self) -> AppResponse:
        """The workspace settings document; data={"settings": WorkspaceSettings}."""
        raise NotImplementedError

    @abstractmethod
    async def get_metrics(self, *, tenant_ids: frozenset[str] | None) -> AppResponse:
        """Dashboard counters within ``tenant_ids``; see control_plane.metrics.summarize_metrics."""
        raise NotImplementedError
