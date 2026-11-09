"""Application use cases backed by provider-neutral runtime gateways."""

from __future__ import annotations

import asyncio
import getpass
import logging
import socket
import uuid
from collections.abc import AsyncGenerator, Mapping
from typing import cast, get_args

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ports.events import EventBusPort
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories
from harborrag_runtime.config.connectors.importing import import_connectors_once
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config_store.defaults import registry_for

from ..chat import ChatApplicationService, ChatClientMixin
from ..chat.scope import ControlPlaneConnectionCatalog
from ..control_plane.connection_views import IngestionLinks
from ..control_plane.connections import ConnectionService
from ..control_plane.effect_recovery import (
    EFFECT_RECOVERY_LEASE_NAME,
    EFFECT_RECOVERY_LEASE_TTL_SECONDS,
    recover_pending_control_plane_effects,
)
from ..control_plane.projects import ProjectService
from ..control_plane.settings_import import SeedOutcome, SettingsImporter, seed_settings_once
from ..control_plane.settings_service import SettingsService
from ..errors import IngestionNotFoundError, failure_response
from ..ingestion.client import PublicIngestionClientMixin
from ..ingestion.connection_sources import ConnectionSourceInputBuilder
from ..ingestion.direct import DirectIngestionClientMixin, DirectIngestionOperations
from ..ingestion.durable import DurableIngestionOperations
from ..ingestion.failure_inbox import FailureInboxService
from ..ingestion.operations import ControlAction, IngestionOperations
from ..ingestion.ports import PublicTaskStore, SourceInputBuilder
from ..ingestion.presenters import STATUS_NAMES, TERMINAL_STATES
from ..ingestion.progress_bridge import (
    LEASE_NAME,
    LEASE_TTL_SECONDS,
    RUN_COMPLETED,
    STREAMED_EVENTS,
    event_prefix,
    sync_ingestion_progress,
    sync_run,
)
from ..ingestion.service import IngestionApplicationService
from ..memory import (
    ConversationDirectoryService,
    ConversationSessionService,
    MemoryAdminClientMixin,
    MemoryAdministrationService,
    MemoryExtractionQueue,
    conversation_memory,
    long_term_memories,
    long_term_memory_index,
    model_usage_records,
    project_lookup,
)
from ..memory.locks import SessionLocks
from ..ports import BaseAppService
from ..retrieval.client import RetrievalClientMixin
from ..scheduling.service import ScheduleApplicationService
from ..schemas import AppResponse
from .factories import AppServiceFactories
from .resources import AppResources

REMOVAL_RECOVERY_LEASE_NAME = "connection_removal_recovery"

logger = logging.getLogger("harborrag.app.workflow_control.composition.service")


def _operator() -> str:
    """The local operator, for the run's event trail; access control is the DB credential."""
    return f"{getpass.getuser()}@{socket.gethostname()}"


class AppService(
    ChatClientMixin,
    MemoryAdminClientMixin,
    PublicIngestionClientMixin,
    DirectIngestionClientMixin,
    RetrievalClientMixin,
    BaseAppService,
):
    """Keep transport concerns outside the canonical ingestion execution path."""

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
        selected = factories or AppServiceFactories()
        self._runtime_config = selected.ingestion_description(self._settings)
        self._source_input_builder: SourceInputBuilder = (
            selected.source_input_builder
            or ConnectionSourceInputBuilder(self._control_plane, selected.source_preparation)
        )
        self._resources = AppResources(
            self._settings,
            factories=selected,
            composition=composition,
        )
        self._public_ingestions = IngestionApplicationService(
            self._settings,
            client_provider=self._resources.runtime_client,
            task_store_provider=self._resources.public_task_store,
            source_input_builder=self._source_input_builder,
        )
        self._schedules = ScheduleApplicationService(
            self._settings,
            backend_provider=self._resources.schedule_backend,
            source_input_builder=self._source_input_builder,
        )
        memory = conversation_memory(self._composition)
        memories = long_term_memories(self._composition)
        index = long_term_memory_index(self._composition)
        projects = project_lookup(self._composition)
        usage = model_usage_records(self._composition)
        self._sessions = ConversationSessionService(memory)
        locks = SessionLocks(memory)
        # Extraction costs a model call, so it runs off the request path; the
        # API lifespan starts and drains the pool (the CLI never starts it).
        self._extraction: MemoryExtractionQueue | None = (
            MemoryExtractionQueue(
                runtime_provider=self._resources.runtime_sdk,
                memories=memories,
                index=index,
            )
            if self._settings.memory_extraction_enabled and memories is not None
            else None
        )
        self._memory_admin = MemoryAdministrationService(
            conversations=memory,
            memories=memories,
            index=index,
            pending_effects=getattr(
                getattr(composition, "control_plane", None), "pending_effects", None
            ),
        )
        self._conversation_directory = ConversationDirectoryService(memory, self._memory_admin)
        self._chat = ChatApplicationService(
            self._resources.runtime_sdk,
            self._settings,
            memory=memory,
            locks=locks,
            projects=projects,
            memories=memories,
            index=index,
            extraction=self._extraction,
            usage=usage,
            config=getattr(getattr(composition, "control_plane", None), "config", None),
            catalog=ControlPlaneConnectionCatalog(
                lambda: self._control_plane().connections, self._resources.task_registry
            ),
        )
        self._durable = DurableIngestionOperations(
            self._settings,
            runtime_client=self._resources.runtime_client,
            task_registry=self._resources.task_registry,
            source_input_builder=self._source_input_builder,
        )
        self._direct = DirectIngestionOperations(self._resources.runtime_sdk)
        self._connection_service = ConnectionService(
            control_plane=self._control_plane,
            runs=self._resources.task_registry,
            ingestions=self._public_ingestions,
            removal=self._resources.connection_removal,
            instance_id=self._instance_id,
        )
        self._ingestion_operations = IngestionOperations(
            connections=lambda: self._control_plane().connections,
            runs=self._resources.task_registry,
            actions=self._public_ingestions,
            links=IngestionLinks(
                temporal_ui_url=self._settings.temporal_ui_url,
                temporal_namespace=self._settings.temporal_namespace,
                grafana_url=self._settings.grafana_url,
            ),
        )
        self._failure_inbox = FailureInboxService(
            connections=lambda: self._control_plane().connections,
            runs=self._resources.task_registry,
            actions=self._public_ingestions,
        )
        self._project_service = ProjectService(
            control_plane=self._control_plane,
            runs=self._resources.task_registry,
            connections=self._connection_service,
        )
        settings_registry = registry_for(self._settings)
        self._settings_service = SettingsService(
            control_plane=self._control_plane, registry=settings_registry
        )
        self._settings_importer = SettingsImporter(
            control_plane=self._control_plane, registry=settings_registry
        )

    @property
    def ingestion_operations(self) -> IngestionOperations:
        """Run reads behind the ``/v1/admin/ingestions`` routes."""
        return self._ingestion_operations

    @property
    def failure_inbox(self) -> FailureInboxService:
        """Failure inbox behind the ``/v1/admin/failures`` routes."""
        return self._failure_inbox

    @property
    def connection_service(self) -> ConnectionService:
        """Connection use cases behind the ``/v1/admin`` connection routes."""
        return self._connection_service

    @property
    def settings_service(self) -> SettingsService:
        """Runtime settings use cases behind the ``/v1/admin/settings`` routes."""
        return self._settings_service

    @property
    def settings_importer(self) -> SettingsImporter:
        """Settings file import, export and seeding."""
        return self._settings_importer

    @property
    def project_service(self) -> ProjectService:
        """Project use cases behind the ``/v1/admin/projects`` routes."""
        return self._project_service

    async def import_connectors_once(self) -> int:
        """Import ``connectors.yaml`` on the first start of a fresh install."""
        control_plane = self._control_plane()
        plan = await import_connectors_once(
            self._settings.connector_config_path,
            tenant_id=self._settings.ingestion_tenant_id,
            projects=control_plane.projects,
            connections=control_plane.connections,
            settings=control_plane.settings,
        )
        return len(plan.writes) if plan is not None else 0

    async def seed_settings_once(self) -> SeedOutcome:
        """Seed ``config/settings.yaml`` into an empty settings log, else report drift."""
        return await seed_settings_once(
            self._settings_importer,
            self._settings.settings_seed_path,
            tenant_id=self._settings.ingestion_tenant_id,
        )

    async def resume_connection_removals(self) -> int:
        """Finish removals a previous process left running; one API process at a time."""
        acquired = await self._control_plane().leases.try_acquire(
            REMOVAL_RECOVERY_LEASE_NAME,
            self._instance_id,
            ttl_seconds=EFFECT_RECOVERY_LEASE_TTL_SECONDS,
        )
        if not acquired:
            return 0
        return await self._connection_service.resume_deletions() + (
            await self._project_service.resume_deletions()
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

    async def runtime_health(self) -> AppResponse:
        try:
            async with asyncio.timeout(self._runtime_config.health_timeout_seconds):
                client = await self._resources.runtime_client()
                ready = await client.health()
            return AppResponse(
                ready,
                {
                    "runtime": {
                        "provider": self._runtime_config.provider,
                        "ready": ready,
                        **self._runtime_config.details,
                    }
                },
                None if ready else "Ingestion workflow service is not ready",
            )
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return failure_response(logger, exc, "check ingestion runtime health")

    async def sync_declared_schedules(self) -> dict[str, int] | None:
        return await self._schedules.sync_declared()

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
        return await recover_pending_control_plane_effects(
            self._control_plane(),
            limit=limit,
            erasure_handler=self._memory_admin.recover_erasure,
            connection_busy=self._connection_service.connection_busy,
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
            store,
            self._resources.event_bus(),
            still_leader=still_leader,
        )

    async def stream_ingestion_events(
        self, task_id: str, *, after_seq: int | None = None
    ) -> AsyncGenerator[HarborEvent, None]:
        """Backlog replay then a live tail of a task's events, ending after ``run.completed``.

        ``after_seq`` resumes a reconnecting client (Last-Event-ID) after its last-seen
        sequence. The backlog is read in pages until a short page arrives. The stream
        subscribes before reading it so nothing published in between is dropped; an event
        delivered by both is yielded once, so sequences only increase.

        The stream finishes a run itself: after the backlog and on every idle check it
        re-reads the run, and if the run ended without a stored ``run.completed`` it appends
        one (at most once across streams and replicas) and replays it. A watcher therefore
        never depends on the progress bridge having seen the run end.

        ``live`` is closed in a ``finally`` so an abandoned caller deterministically
        unsubscribes instead of waiting for the event bus's GC finalizer.
        """
        task = await self._public_ingestions.get_task(task_id)
        store = await self._resources.public_task_store()
        bus = self._resources.event_bus()
        terminal_names = {STATUS_NAMES[state] for state in TERMINAL_STATES}
        live = None if task["status"] in terminal_names else bus.subscribe(event_prefix(task_id))
        cursor = _StreamCursor(task_id, after_seq)
        tail = None if live is None else _LiveTail(live)
        check_ended = True
        try:
            async for event in _backlog(store, cursor):
                yield event
            while not cursor.done:
                if check_ended and await _complete_if_ended(store, bus, task_id):
                    async for event in _backlog(store, cursor):
                        yield event
                    return
                if tail is None:
                    return
                live_event = await tail.next(_END_CHECK_SECONDS)
                check_ended = live_event is None
                if live_event is not None:
                    async for event in _deliver(store, cursor, live_event):
                        yield event
        finally:
            if tail is not None:
                await tail.close()

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
        return await self._durable.start_ingestion(
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
        return await self._durable.ingestion_status(run_id)

    async def ingestion_result(self, run_id: str) -> AppResponse:
        return await self._durable.ingestion_result(run_id)

    async def control_ingestion(self, run_id: str, action: str) -> AppResponse:
        """Pause, resume or cancel a run on the same rules as the HTTP routes.

        The acting principal is the local operator; a run the database does not know
        is not found, as over HTTP.
        """
        try:
            if action not in get_args(ControlAction):
                raise ValueError(f"unsupported ingestion action: {action!r}")
            task = await (await self._resources.task_registry()).get(run_id)
            tenant_id = task.request.get("tenant_id") if task is not None else None
            if not isinstance(tenant_id, str) or not tenant_id:
                raise IngestionNotFoundError(f"ingestion {run_id!r} not found")
            await self._ingestion_operations.act(
                tenant_id=tenant_id,
                ingestion_id=run_id,
                action=cast(ControlAction, action),
                actor=_operator(),
            )
            logger.info("Applied %r to ingestion run %r", action, run_id)
            return AppResponse(True, {"run_id": run_id, "action": action})
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            return failure_response(logger, exc, "apply %r to ingestion run %r", action, run_id)

    async def start_config_provider(self) -> None:
        await self._composition.start()

    async def aclose(self) -> None:
        try:
            await self._connection_service.aclose()
            await self.drain_memory_extraction()
        finally:
            try:
                await self._resources.aclose()
            finally:
                await self._composition.aclose()


_END_CHECK_SECONDS = 2.0
_REPLAY_PAGE_LIMIT = 500


class _LiveTail:
    """A live subscription read with a timeout that never cancels the subscription itself."""

    def __init__(self, live: AsyncGenerator[HarborEvent, None]) -> None:
        self._live = live
        self._pending: asyncio.Future[HarborEvent] | None = None

    async def next(self, timeout_seconds: float) -> HarborEvent | None:
        """Return the next live event, or ``None`` when none arrived within the timeout."""
        if self._pending is None:
            self._pending = asyncio.ensure_future(anext(self._live))
        done, _ = await asyncio.wait({self._pending}, timeout=timeout_seconds)
        if not done:
            return None
        event, self._pending = self._pending.result(), None
        return event

    async def close(self) -> None:
        """Cancel the pending read, then close the subscription."""
        if self._pending is not None:
            self._pending.cancel()
            await asyncio.gather(self._pending, return_exceptions=True)
        await self._live.aclose()


class _StreamCursor:
    """Where a run's event stream stands: the last sequence seen and whether it has ended."""

    def __init__(self, task_id: str, after_seq: int | None) -> None:
        self.last_seq = after_seq
        self.done = False
        self._completed = f"{event_prefix(task_id)}{RUN_COMPLETED}"
        self.task_id = task_id

    def take(self, event: HarborEvent) -> bool:
        """Advance past ``event``; return whether it should be sent.

        ``run.completed`` is always sent once, whatever its sequence, because a duplicate with
        a higher sequence can overtake it. Only the four explorer events are sent.
        """
        if event.name == self._completed:
            first = not self.done
            self.done = True
            self._advance(event)
            return first
        if event.seq is not None and self.last_seq is not None and event.seq <= self.last_seq:
            return False
        self._advance(event)
        return event.name.removeprefix(event_prefix(self.task_id)) in STREAMED_EVENTS

    def skipped_before(self, event: HarborEvent) -> bool:
        """Whether stored events lie between the last one seen and ``event``."""
        return event.seq is not None and event.seq > (self.last_seq or 0) + 1

    def _advance(self, event: HarborEvent) -> None:
        if event.seq is not None and (self.last_seq is None or event.seq > self.last_seq):
            self.last_seq = event.seq


async def _deliver(
    store: PublicTaskStore, cursor: _StreamCursor, live_event: HarborEvent
) -> AsyncGenerator[HarborEvent, None]:
    """Yield ``live_event``, first reading back from the store any events the tail skipped."""
    if cursor.skipped_before(live_event):
        async for event in _backlog(store, cursor):
            yield event
    if cursor.take(live_event):
        yield live_event


async def _backlog(
    store: PublicTaskStore, cursor: _StreamCursor
) -> AsyncGenerator[HarborEvent, None]:
    """Yield the stored events past the cursor that should be sent, up to ``run.completed``."""
    async for event in _replay(store, cursor.task_id, cursor.last_seq):
        if cursor.take(event):
            yield event
        if cursor.done:
            return


async def _replay(
    store: PublicTaskStore, task_id: str, after_seq: int | None
) -> AsyncGenerator[HarborEvent, None]:
    """Yield the stored events after ``after_seq``, page by page until a short page."""
    cursor = after_seq
    while True:
        page = await store.list_task_events(task_id, after_seq=cursor, limit=_REPLAY_PAGE_LIMIT)
        for event in page:
            yield event
        if len(page) < _REPLAY_PAGE_LIMIT:
            return
        cursor = page[-1].seq


async def _complete_if_ended(store: PublicTaskStore, bus: EventBusPort, task_id: str) -> bool:
    """Store and publish the run's missing events when it has ended; whether it had."""
    task = await store.get(task_id)
    if task is None or task.status not in TERMINAL_STATES:
        return False
    await sync_run(store, bus, task)
    return True
