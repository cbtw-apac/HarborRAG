"""Application use cases backed by the HarborRAG Temporal runtime client."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Mapping

from harborrag_core.contracts.errors import HarborNotFoundError, HarborUnavailableError
from harborrag_core.contracts.events import HarborEvent
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig

from ..agent import AgentApplicationService, AgentClientMixin
from ..chat import ChatApplicationService, ChatClientMixin
from ..control_plane.effect_recovery import (
    EFFECT_RECOVERY_LEASE_NAME,
    EFFECT_RECOVERY_LEASE_TTL_SECONDS,
)
from ..control_plane.reads import ControlPlaneReadsMixin
from ..control_plane.writes import ControlPlaneWritesMixin
from ..errors import failure_response
from ..ingestion.client import PublicIngestionClientMixin
from ..ingestion.presenters import STATUS_NAMES, TERMINAL_STATES
from ..ingestion.progress_bridge import LEASE_NAME, LEASE_TTL_SECONDS, sync_ingestion_progress
from ..ingestion.service import IngestionApplicationService
from ..ingestion.temporal import TemporalIngestionOperations
from ..memory import ConversationSessionService, agent_run_checkpoints, conversation_memory
from ..ports import BaseAppService
from ..retrieval.client import RetrievalClientMixin
from ..retrieval.graph import GraphRetrievalService
from ..schemas import AppResponse
from .factories import AppServiceFactories
from .resources import AppResources

logger = logging.getLogger("harborrag.app.workflow_control.composition.service")


class AppService(
    ControlPlaneReadsMixin,
    ControlPlaneWritesMixin,
    AgentClientMixin,
    ChatClientMixin,
    PublicIngestionClientMixin,
    RetrievalClientMixin,
    BaseAppService,
):
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
        # Identifies this process as a lease holder (ingestion progress bridge);
        # stable for the process's lifetime, unique across concurrently running ones.
        self._instance_id = uuid.uuid4().hex
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
            async with asyncio.timeout(self._runtime_config.health_timeout_seconds):
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

    async def recover_pending_control_plane_effects(self, *, limit: int = 100) -> int:
        """Drain one pass of the durable secret-retirement/audit-logging outbox.

        Gated by the same DB-backed lease pattern as sync_ingestion_progress:
        when the API runs as more than one process, only the lease holder
        drains the queue this pass -- every other instance's pass is a
        no-op, so two processes never replay the same pending effect at
        once (which could double-log the same activity entry).
        """
        acquired = await self._control_plane().leases.try_acquire(
            EFFECT_RECOVERY_LEASE_NAME,
            self._instance_id,
            ttl_seconds=EFFECT_RECOVERY_LEASE_TTL_SECONDS,
        )
        if not acquired:
            return 0
        return await ControlPlaneWritesMixin.recover_pending_control_plane_effects(
            self, limit=limit
        )

    async def sync_ingestion_progress(self) -> int:
        """One poll tick fanning active tasks' progress out via the event bus.

        Gated by a DB-backed lease (single-API-process deployment constraint,
        see deploy/README.md): when the API runs as more than one process,
        only the instance currently holding the lease ticks -- every other
        instance's tick is a no-op, so their progress bridges never race each
        other into double-appending the same task's event log.

        This upfront acquire only proves ownership at this instant, not for
        the tick's whole duration -- list_active() is paginated, so with
        enough active tasks a tick can run long enough to outlive the
        lease's ttl. ``still_leader`` re-runs this same try_acquire (renewing
        the ttl) before every page sync_ingestion_progress reads, so a tick
        that overruns loses the lease and stops instead of continuing to
        write alongside whichever process took over next.
        """
        acquired = await self._control_plane().leases.try_acquire(
            LEASE_NAME, self._instance_id, ttl_seconds=LEASE_TTL_SECONDS
        )
        if not acquired:
            return 0
        store = await self._resources.public_task_store()

        async def still_leader() -> bool:
            return await self._control_plane().leases.try_acquire(
                LEASE_NAME, self._instance_id, ttl_seconds=LEASE_TTL_SECONDS
            )

        return await sync_ingestion_progress(
            store, self._resources.event_bus(), still_leader=still_leader
        )

    async def stream_ingestion_events(
        self, task_id: str, *, after_seq: int | None = None
    ) -> AsyncGenerator[HarborEvent, None]:
        """Backlog replay then a live tail of a task's progress events.

        ``after_seq`` resumes a reconnecting client (Last-Event-ID) after its
        last-seen sequence instead of replaying the full backlog. Subscribes
        before reading the backlog so nothing published in the gap between
        the two is ever dropped (a harmless duplicate at worst, since every
        progress payload is a full snapshot). Stops after a "task.<id>.done"
        event, or immediately after the backlog if the task's persisted
        state is already terminal, since the progress bridge only ever
        touches active (PENDING/RUNNING) tasks.

        ``live`` is closed in a ``finally`` so an abandoned caller -- e.g.
        the SSE route's client disconnecting while this generator is
        suspended mid-backlog, before it ever reaches the live tail --
        deterministically unsubscribes instead of leaving the subscription
        registered until the event bus's GC finalizer happens to run.
        """
        store = await self._resources.public_task_store()
        task = await store.get(task_id)
        if task is None:
            raise HarborNotFoundError("Ingestion task was not found")
        live = None
        terminal_names = {STATUS_NAMES[state] for state in TERMINAL_STATES}
        if STATUS_NAMES[task.status] not in terminal_names:
            live = self._resources.event_bus().subscribe(f"task.{task_id}.")
        try:
            for event in await store.list_task_events(task_id, after_seq=after_seq):
                yield event
                if event.name.endswith(".done"):
                    return
            if live is None:
                return
            async for event in live:
                yield event
                if event.name.endswith(".done"):
                    return
        finally:
            if live is not None:
                await live.aclose()

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
        batch_size: int | None = None,
        document_concurrency: int | None = None,
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
            batch_size=batch_size,
            document_concurrency=document_concurrency,
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
