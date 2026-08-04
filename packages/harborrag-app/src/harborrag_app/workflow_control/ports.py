from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping

from harborrag_core.retrieval import GraphPathQuery, GraphSubgraphQuery, GraphTripletQuery
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.sdk import RetrievalLane

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

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        system: ChatPrompt | None = None,
    ) -> AppResponse:
        """Generate one retrieval-grounded chat completion."""

        raise NotImplementedError

    def chat_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        system: ChatPrompt | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        """Stream one retrieval-grounded chat completion as ``{"kind": ...}`` events."""

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
