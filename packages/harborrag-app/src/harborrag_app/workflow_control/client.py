"""Application use cases backed by the HarborRAG Temporal runtime client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.retrieval import (
    GraphNeighborhoodQuery,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.projection_admin import ProjectionAdministrationService
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig, RetrievalLane
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.schemas import SourceIngestionInput
from harborrag_runtime.temporal.submission import (
    SourceSubmission,
    build_source_input,
)
from harborrag_runtime.temporal.task_registry import IngestionTaskRegistry

from .agent import AgentApplicationService, AgentClientMixin
from .app_resources import AppResources
from .chat import ChatApplicationService
from .chat_client import ChatClientMixin
from .errors import failure_response
from .graph_retrieval import GraphRetrievalService
from .ingestion_models import IngestionCreateCommand
from .ingestion_service import IngestionApplicationService, PublicTaskStore
from .memory import ConversationSessionService, agent_run_checkpoints, conversation_memory
from .ports import BaseAppService
from .reads import ControlPlaneReadsMixin
from .retrieval_query import retrieve
from .schemas import AppResponse
from .temporal_ingestion import TemporalIngestionOperations

type ClientFactory = Callable[
    [TemporalRuntimeConfig],
    Awaitable[IngestionTemporalClient],
]
type RetrievalRuntimeFactory = Callable[[RuntimeSettings], HarborRAG]
type SourceInputBuilder = Callable[
    [RuntimeSettings, SourceSubmission],
    SourceIngestionInput,
]
type ProjectionAdminFactory = Callable[
    [RuntimeSettings],
    ProjectionAdministrationService,
]
type CloseOperation = Callable[[], Awaitable[None]]


class TaskRegistry(PublicTaskStore, Protocol):
    async def close(self) -> None: ...


type TaskRegistryFactory = Callable[[RuntimeSettings], Awaitable[TaskRegistry]]

logger = logging.getLogger("harborrag.app.workflow_control.client")


def _retrieval_runtime(settings: RuntimeSettings) -> HarborRAG:
    return HarborRAG(HarborRAGConfig(runtime=settings))


@dataclass(frozen=True, slots=True)
class AppServiceFactories:
    """Collaborator factories, grouped so composition stays overridable in tests."""

    client: ClientFactory = IngestionTemporalClient.connect
    retrieval_runtime: RetrievalRuntimeFactory = _retrieval_runtime
    source_input_builder: SourceInputBuilder = build_source_input
    task_registry: TaskRegistryFactory = IngestionTaskRegistry.connect
    projection_admin: ProjectionAdminFactory = ProjectionAdministrationService


class AppService(ControlPlaneReadsMixin, AgentClientMixin, ChatClientMixin, BaseAppService):
    """Keep transport concerns outside the canonical Temporal ingestion path."""

    def __init__(
        self,
        composition: CompositionRoot,
        settings: RuntimeSettings | None = None,
        *,
        factories: AppServiceFactories | None = None,
    ) -> None:
        self._composition = composition
        self._settings = settings or RuntimeSettings()
        self._runtime_config = TemporalRuntimeConfig.from_settings(self._settings)
        selected = factories or AppServiceFactories()
        self._source_input_builder = selected.source_input_builder
        self._resources = AppResources(
            self._settings,
            runtime_config=self._runtime_config,
            factories=selected,
        )
        self._public_ingestions = IngestionApplicationService(
            self._settings,
            client_provider=self._resources.runtime_client,
            task_store_provider=self._resources.public_task_store,
            source_input_builder=self._source_input_builder,
        )
        memory = conversation_memory(self._composition)
        self._sessions = ConversationSessionService(memory)
        self._chat = ChatApplicationService(
            self._resources.runtime_sdk,
            self._settings,
            memory=memory,
        )
        self._agent = AgentApplicationService(
            self._resources.runtime_sdk,
            memory=memory,
            runs=agent_run_checkpoints(self._composition),
        )
        self._graph = GraphRetrievalService(self._resources.runtime_sdk)
        self._temporal = TemporalIngestionOperations(
            self._settings,
            runtime_client=self._resources.runtime_client,
            task_registry=self._resources.task_registry,
            source_input_builder=self._source_input_builder,
        )

    def _control_plane(self) -> ControlPlaneRepositories:
        control_plane = self._composition.control_plane
        if control_plane is None:
            raise HarborUnavailableError("control-plane database is not configured")
        return control_plane

    def health(self) -> AppResponse:
        diagnostics = self._composition.diagnostics()
        runtime = diagnostics.get("runtime")
        ready = bool(runtime.get("ready")) if isinstance(runtime, dict) else False
        return AppResponse(
            ok=ready,
            data={"diagnostics": diagnostics},
            error=None if ready else "runtime not ready",
        )

    def ingest_once(self) -> AppResponse:
        return AppResponse(
            False,
            error="use 'harborrag ingest start' to submit the Temporal ingestion workflow",
        )

    async def runtime_health(self) -> AppResponse:
        try:
            async with asyncio.timeout(self._settings.temporal_health_timeout_seconds):
                client = await self._resources.runtime_client()
                ready = await client.health()
            return AppResponse(
                ready,
                {
                    "runtime": {
                        "provider": "temporal",
                        "ready": ready,
                        "target": self._runtime_config.connection.target,
                        "namespace": self._runtime_config.connection.namespace,
                    }
                },
                None if ready else "Temporal workflow service is not ready",
            )
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return failure_response(logger, exc, "check Temporal runtime health")

    async def submit(
        self,
        command: IngestionCreateCommand,
        *,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        return await self._public_ingestions.submit(
            command,
            idempotency_key=idempotency_key,
        )

    async def get_task(self, task_id: str) -> dict[str, object]:
        return await self._public_ingestions.get_task(task_id)

    async def recover_pending_submissions(self, *, limit: int = 100) -> int:
        return await self._public_ingestions.recover_pending_submissions(limit=limit)

    async def list_documents(
        self,
        *,
        task_id: str,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        return await self._public_ingestions.list_documents(
            task_id=task_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )

    async def cancel(self, task_id: str) -> dict[str, object]:
        return await self._public_ingestions.cancel(task_id)

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: list[str],
    ) -> dict[str, object]:
        return await self._public_ingestions.retry_failures(
            task_id=task_id,
            document_ids=document_ids,
        )

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
        return await retrieve(
            self._resources.runtime_sdk,
            self._settings,
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            top_k=top_k,
            filters=filters,
            lane=lane,
            observe_graph=observe_graph,
            include_content=include_content,
            include_metadata=include_metadata,
            score_threshold=score_threshold,
        )

    async def retrieve_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._graph.triplets(query, tenant_id=tenant_id, principal_id=principal_id)

    async def retrieve_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._graph.paths(query, tenant_id=tenant_id, principal_id=principal_id)

    async def retrieve_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._graph.subgraph(query, tenant_id=tenant_id, principal_id=principal_id)

    async def retrieve_graph_neighborhood(
        self,
        query: GraphNeighborhoodQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._graph.neighborhood(query, tenant_id=tenant_id, principal_id=principal_id)

    async def start_ingestion(  # noqa: PLR0913 - stable service port
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
        return await self._temporal.start_ingestion(
            tenant_id=tenant_id,
            connector_name=connector_name,
            run_id=run_id,
            connection_id=connection_id,
            source_scope_id=source_scope_id,
            path=path,
            pattern=pattern,
            recursive=recursive,
            updated_after=updated_after,
            max_artifacts=max_artifacts,
            include_attachments=include_attachments,
            filters=filters,
            force_reprocess=force_reprocess,
            wait=wait,
        )

    async def ingestion_status(self, run_id: str) -> AppResponse:
        return await self._temporal.ingestion_status(run_id)

    async def ingestion_result(self, run_id: str) -> AppResponse:
        return await self._temporal.ingestion_result(run_id)

    async def control_ingestion(self, run_id: str, action: str) -> AppResponse:
        return await self._temporal.control_ingestion(run_id, action)

    async def projection_inventory(self, tenant: str) -> dict[str, object]:
        return (await self._resources.projection_administration().inspect(tenant)).as_dict()

    async def delete_projections(
        self,
        tenant: str,
        *,
        confirmation: str,
        stores: frozenset[str],
    ) -> dict[str, object]:
        result = await self._resources.projection_administration().delete(
            tenant,
            confirmation=confirmation,
            stores=stores,
        )
        return result.as_dict()

    async def aclose(self) -> None:
        try:
            await self._resources.aclose()
        finally:
            await self._composition.aclose()
