"""Connection use cases: read, write, test, sync and delete one configured data source.

Handles:
- ``put`` with whole-config replacement, secret keep/rotate/clear and ETag preconditions
- ``test`` of form values or the saved connection, saving nothing
- ``sync`` with one active ingestion per connection
- ``delete`` as a background removal from every store, resumed after a restart

Does NOT speak HTTP; routes translate these results and errors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from uuid import uuid4

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborCapabilityError,
    HarborConflictError,
    HarborError,
    HarborImmutableFieldError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
    HarborUnavailableError,
)
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.connection import (
    Connection,
    ConnectionBuilder,
    ConnectionStatus,
    require_connection_id,
)
from harborrag_core.domain.job import Job, JobStatus
from harborrag_core.ingestion import IngestionTask
from harborrag_core.security.secret_refs import SecretSource, all_refs, stored_refs
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.models import (
    ConnectionValidationError,
    FieldIssue,
    SourceTypeSpec,
    ValidatedConnection,
    source_types,
)
from harborrag_runtime.config.connectors.models.builder import validate_connection
from harborrag_runtime.config.connectors.models.spec import METHOD_KEY
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.connections import (
    CredentialSecretWriter,
    SecretUnavailableError,
    default_secret_resolver,
    reference_issues,
    reuses_stored_reference,
)
from harborrag_runtime.ingestion.connection_checks import (
    ConnectionTestReport,
    run_connection_checks,
    timed_out_report,
)
from harborrag_runtime.ingestion.connections import (
    ConnectionUnavailableError,
    build_connector_from_parts,
)

from ..ingestion.models import IngestionCreateCommand
from .active_runs import active_run
from .connection_views import ingestion_view, read_connection_views, resolve_any_version
from .effect_recovery import defer_retirement, log_activity, retire_refs

if TYPE_CHECKING:
    from harborrag_runtime.composition.connection_removal import ConnectionRemoval

logger = logging.getLogger("harborrag.app.workflow_control.control_plane.connections")

TEST_TIMEOUT_SECONDS = 15.0
_DELETE_CONNECTION_JOB: Final = "delete_connection"
TEST_REQUEST_TIMEOUT_SECONDS = 10.0
TEST_WORKERS = 4
_TEST_EXECUTOR = ThreadPoolExecutor(max_workers=TEST_WORKERS, thread_name_prefix="connection-test")
SYNC_LEASE_TTL_SECONDS = 5.0

WriteOrigin = Literal["api", "operator"]
"""Who writes: ``api`` callers may not set new ``env:`` references or operator-only
fields such as a worker file path; the CLI (``operator``) may."""


class ConnectionRuns(Protocol):
    """Ingestion runs and published-document counts of one connection."""

    async def latest_for_connection(
        self, *, tenant_id: str, connection_id: str
    ) -> IngestionTask | None: ...

    async def active_for_connection(
        self, *, tenant_id: str, connection_id: str
    ) -> IngestionTask | None: ...

    async def connection_document_count(self, *, tenant_id: str, connection_id: str) -> int: ...

    async def get(self, task_id: str) -> IngestionTask | None: ...

    async def progress(self, task_id: str) -> dict[str, int]: ...

    async def progress_for_tasks(self, task_ids: Sequence[str]) -> dict[str, dict[str, int]]: ...


class IngestionSubmitter(Protocol):
    """Start an ingestion through the shared submission path; tell when a run ended unrecorded."""

    async def submit(
        self, command: IngestionCreateCommand, *, idempotency_key: str | None
    ) -> dict[str, object]: ...

    async def settle_if_ended(self, task_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ConnectionWrite:
    """The body of a connection ``PUT`` or ``test``."""

    source_type: str
    credentials: Mapping[str, Any]
    config: Mapping[str, Any]
    name: str | None = None
    tuning: Mapping[str, Any] | None = None

    def effective_tuning(self, existing: Connection | None) -> Mapping[str, Any]:
        """Return the tuning to apply: as sent, else what is stored, else none."""
        if self.tuning is not None:
            return self.tuning
        return existing.tuning if existing else {}


@dataclass(frozen=True, slots=True)
class ConnectionPutResult:
    """A stored connection, whether it was new, and what ``sync_now`` started or why it did not."""

    connection: dict[str, Any]
    created: bool
    version: int
    ingestion: dict[str, Any] | None = None
    sync_error: HarborError | None = None


@dataclass(frozen=True, slots=True)
class ConnectionKey:
    """The public identity of a connection inside one tenant."""

    tenant_id: str
    project_id: str
    connection_id: str


class ConnectionService:
    """Facade over the connection repository, secrets, ingestion runs and removal."""

    def __init__(
        self,
        *,
        control_plane: Callable[[], ControlPlaneRepositories],
        runs: Callable[[], Awaitable[ConnectionRuns]],
        ingestions: IngestionSubmitter,
        removal: Callable[[], ConnectionRemoval],
        instance_id: str,
    ) -> None:
        self._control_plane = control_plane
        self._runs = runs
        self._ingestions = ingestions
        self._removal = removal
        self._instance_id = instance_id
        self._background: set[asyncio.Task[None]] = set()
        self._removing: set[tuple[str, str]] = set()

    async def get(self, key: ConnectionKey) -> tuple[dict[str, Any], int]:
        """Return a connection's view and version.

        Raises:
            HarborNotFoundError: No such connection in the tenant.
        """
        connection = await self._require(key)
        return await self._view(connection), connection.version

    async def stored(self, key: ConnectionKey) -> Connection:
        """Return the stored row with the ``source_key`` runs use; not found as ``get`` raises."""
        return await self._require(key)

    async def list_page(  # noqa: PLR0913 - the spec's list filters plus paging
        self,
        *,
        tenant_id: str,
        project_id: str | None = None,
        source_type: str | None = None,
        status: ConnectionStatus | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return one page of the tenant's connections, needing attention first."""
        canonical = source_types.canonical(source_type) if source_type else None
        connections, next_cursor = await self._control_plane().connections.list(
            tenant_ids=frozenset({tenant_id}),
            project_id=project_id,
            source_type=canonical or source_type,
            status=status,
            limit=limit,
            cursor=cursor,
        )
        views = await read_connection_views(await self._runs(), connections, batch=True)
        return views, next_cursor

    async def searchable(self, *, tenant_id: str, project_id: str) -> list[dict[str, Any]]:
        """Connections of one project as reader filter chips.

        Raises:
            HarborNotFoundError: The project does not exist in the tenant.
        """
        control_plane = self._control_plane()
        tenants = frozenset({tenant_id})
        if await control_plane.projects.get(project_id, tenant_ids=tenants) is None:
            raise HarborNotFoundError(f"project {project_id!r} not found")
        connections, _ = await control_plane.connections.list(
            tenant_ids=tenants, project_id=project_id
        )
        runs = await self._runs()
        return [
            {
                "connection_ref": connection.ref,
                "connection_id": connection.connection_id,
                "source_type": connection.source_type,
                "name": connection.name,
                "documents": await runs.connection_document_count(
                    tenant_id=tenant_id, connection_id=connection.source_key
                ),
            }
            for connection in connections
            if connection.status != "deleting"
        ]

    async def put(  # noqa: PLR0913 - one body plus its preconditions
        self,
        key: ConnectionKey,
        write: ConnectionWrite,
        *,
        actor: str,
        expected_version: int | None = None,
        create_only: bool = False,
        sync_now: bool = False,
        origin: WriteOrigin = "api",
    ) -> ConnectionPutResult:
        """Create or overwrite a connection.

        ``config`` is replaced whole, and so is ``tuning`` unless it is left out,
        which keeps the stored tuning. A body that changes nothing stores nothing:
        the version and ``updated_at`` stay. In ``credentials`` an omitted secret keeps
        its value while the auth method and endpoints stay the same, a new value
        rotates it, a ``{"secret_ref"}`` points it elsewhere and ``null`` clears
        it; replaced stored secrets are deleted once no running ingestion still
        uses them. A failed ``sync_now`` keeps the saved connection.

        Args:
            key: The connection's tenant, project and ``connection_id``.
            write: Source type, name, credentials and config as sent.
            actor: Subject of the caller's token.
            expected_version: ``If-Match`` version, when sent.
            create_only: ``If-None-Match: *`` was sent.
            sync_now: Start an incremental sync after saving.
            origin: ``operator`` lets the write set new ``env:`` references.

        Returns:
            The stored connection, whether it was created, and the started
            ingestion or the error that kept ``sync_now`` from starting one.

        Raises:
            HarborNotFoundError: The project does not exist in the tenant.
            ConnectionValidationError: The type is unknown, a field fails its schema,
                an endpoint changed while a stored secret would be kept, or an ``api``
                write sets a new ``env:`` reference or changes an operator-only field.
            HarborImmutableFieldError: The connection exists with another source type.
            HarborPreconditionFailedError: ``If-Match`` or ``If-None-Match`` does not hold.
            HarborConflictError: Without either, another write landed twice in a row
                between reading the connection and saving it.
            ConnectionUnavailableError: The connection is being deleted.
        """
        guarded = expected_version is None and not create_only
        for attempt in range(2 if guarded else 1):
            try:
                return await self._put_once(
                    key,
                    write,
                    actor=actor,
                    expected_version=expected_version,
                    create_only=create_only,
                    sync_now=sync_now,
                    origin=origin,
                    guarded=guarded,
                )
            except _ConcurrentWriteError:
                logger.info("connection %s changed while saving; attempt %d", key, attempt + 1)
        raise HarborConflictError(
            "the connection changed while it was being saved; read it and send it again"
        )

    async def _put_once(  # noqa: PLR0913 - one body plus its preconditions
        self,
        key: ConnectionKey,
        write: ConnectionWrite,
        *,
        actor: str,
        expected_version: int | None,
        create_only: bool,
        sync_now: bool,
        origin: WriteOrigin,
        guarded: bool,
    ) -> ConnectionPutResult:
        """Run one save; ``guarded`` writes only over the version read here.

        Raises:
            _ConcurrentWriteError: ``guarded`` and another write landed first.
        """
        control_plane = self._control_plane()
        await self._require_project(key)
        require_connection_id(key.connection_id)
        spec = source_types.get(write.source_type)
        existing = await control_plane.connections.get(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
        expected_version = resolve_any_version(
            expected_version, existing.version if existing else None
        )
        _check_preconditions(existing, spec, expected_version, create_only)
        _refuse_operator_fields(spec, write, existing, origin)
        sent, keep = _plan_credentials(write.credentials, existing, spec)
        validated = _validate(
            spec, _effective_credentials(sent, existing, keep), write, existing, key.tenant_id
        )
        written = await CredentialSecretWriter(
            control_plane.secrets,
            tenant_id=key.tenant_id,
            sources=_secret_sources(origin),
        ).apply(
            sent,
            secret_fields=spec.secret_fields,
            existing=existing.credentials if existing else None,
            keep=keep,
        )
        credentials = _merge_secrets(validated.credentials, written.credentials, spec.secret_fields)
        connection = _build(key, existing, spec, write, credentials, validated, actor)
        if existing is not None and not written.created and _unchanged(existing, connection):
            return await self._result(key, existing, actor, created=False, sync_now=sync_now)
        try:
            stored, outcome = await control_plane.connections.put(
                connection,
                expected_version=existing.version if guarded and existing else expected_version,
                create_only=create_only or (guarded and existing is None),
            )
        except HarborPreconditionFailedError as error:
            await retire_refs(control_plane, written.created, tenant_id=key.tenant_id)
            if guarded:
                raise _ConcurrentWriteError from error
            raise
        except Exception:
            await retire_refs(control_plane, written.created, tenant_id=key.tenant_id)
            raise
        await self._retire_replaced(stored, written.retired)
        await self._record(
            stored, "created" if outcome == "created" else "updated", actor, existing
        )
        return await self._result(
            key, stored, actor, created=outcome == "created", sync_now=sync_now
        )

    async def _result(
        self,
        key: ConnectionKey,
        stored: Connection,
        actor: str,
        *,
        created: bool,
        sync_now: bool,
    ) -> ConnectionPutResult:
        """Return the saved connection, after starting the ``sync_now`` ingestion when asked."""
        ingestion: dict[str, Any] | None = None
        sync_error: HarborError | None = None
        if sync_now:
            try:
                ingestion = await self.sync(key, actor=actor)
            except HarborError as error:
                sync_error = error
            except Exception:  # noqa: BLE001 - the connection is saved; report, do not fail
                logger.exception("sync_now could not start an ingestion for %s", key)
                sync_error = HarborUnavailableError(
                    "the ingestion could not start; the connection was saved"
                )
        return ConnectionPutResult(
            connection=await self._view(stored),
            created=created,
            version=stored.version,
            ingestion=ingestion,
            sync_error=sync_error,
        )

    async def test(
        self, key: ConnectionKey, write: ConnectionWrite | None, *, origin: WriteOrigin = "api"
    ) -> dict[str, Any]:
        """Check form values, or the saved connection when ``write`` is None; saves nothing.

        Returns:
            The ``ConnectionTest`` body; ``ok`` is False when a check failed.

        Raises:
            HarborNotFoundError: No body and no saved connection, or the project is missing.
            ConnectionValidationError: The values fail the source type's schema, change an
                endpoint while a stored secret would be kept, name a stored secret the
                field does not hold, or, from ``api``, set a new ``env:`` reference or
                change an operator-only field.
        """
        control_plane = self._control_plane()
        existing = await control_plane.connections.get(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
        if write is None:
            if existing is None:
                raise HarborNotFoundError(f"connection {key.connection_id!r} not found")
            write = ConnectionWrite(
                existing.source_type, existing.credentials, existing.config, tuning=existing.tuning
            )
        else:
            await self._require_project(key)
        spec = source_types.get(write.source_type)
        sent, keep = _plan_credentials(write.credentials, existing, spec)
        effective = _effective_credentials(sent, existing, keep)
        _refuse_operator_fields(spec, write, existing, origin)
        issues = reference_issues(
            sent, existing.credentials if existing else None, sources=_secret_sources(origin)
        )
        if issues:
            raise ConnectionValidationError(issues)
        validated = _validate(spec, effective, write, existing, key.tenant_id)
        try:
            secret_values = await default_secret_resolver(
                control_plane.secrets
            ).resolve_credentials(
                {k: v for k, v in effective.items() if isinstance(v, Mapping)},
                tenant_id=key.tenant_id,
            )
        except SecretUnavailableError as unavailable:
            report = ConnectionTestReport()
            report.passed("schema")
            report.secret_unavailable(unavailable)
            return report.as_dict()
        report = await self._run_checks(spec, validated, effective, secret_values, key.tenant_id)
        return report.as_dict()

    async def sync(  # noqa: PLR0913 - the caller, its type and the run options
        self,
        key: ConnectionKey,
        *,
        actor: str,
        actor_type: str = "user",
        mode: str = "incremental",
        item_refs: tuple[str, ...] = (),
        updated_after: str | None = None,
    ) -> dict[str, Any]:
        """Start an ingestion of the connection, recording ``actor`` and ``actor_type`` on the run.

        Returns:
            The spec's ``Ingestion`` object of the started run.

        Raises:
            HarborNotFoundError: No such connection in the tenant.
            HarborAlreadyRunningError: An ingestion is active; ``details.ingestion_id`` names it.
            HarborCapabilityError: ``item_refs`` was sent; per-item syncs are not supported yet.
            ConnectionUnavailableError: The connection is being deleted.
            ConnectionValidationError: The stored values fail the schema or the connector.
        """
        if item_refs:
            raise HarborCapabilityError("syncing selected items is not supported yet")
        connection = await self._require(key)
        if connection.status == "deleting":
            raise ConnectionUnavailableError(f"connection {connection.ref!r} is being deleted")
        validate_connection(
            source_types.get(connection.source_type),
            connection.credentials,
            connection.config,
            connection.tuning,
            tenant_id=connection.tenant_id,
        )
        runs = await self._runs()
        await self._refuse_if_running(key.tenant_id, connection.source_key)
        lease = f"connection-sync:{key.tenant_id}:{connection.source_key}"
        holder = f"{self._instance_id}:{uuid4().hex}"
        if not await self._control_plane().leases.try_acquire(
            lease, holder, ttl_seconds=SYNC_LEASE_TTL_SECONDS
        ):
            await self._refuse_if_running(key.tenant_id, connection.source_key)
            raise HarborAlreadyRunningError(
                "an ingestion is starting for this connection", details={"ingestion_id": None}
            )
        await self._refuse_if_running(key.tenant_id, connection.source_key)
        accepted = await self._ingestions.submit(
            IngestionCreateCommand(
                tenant_id=key.tenant_id,
                connection_id=connection.source_key,
                force_reprocess=mode == "force",
                public_request={
                    "connection": connection.ref,
                    "mode": mode,
                    "updated_after": updated_after,
                    "actor": actor,
                },
                updated_after=updated_after,
                actor=actor,
                actor_type=actor_type,
            ),
            idempotency_key=None,
        )
        task = await runs.get(str(accepted["task_id"]))
        if task is None:
            raise HarborUnavailableError("the ingestion was accepted but cannot be read back")
        await self._record(connection, "synced", actor, None, summary=f"Started a {mode} sync")
        return ingestion_view(
            task,
            connection=connection,
            progress=await runs.progress(task.task_id),
            mode=mode,
        )

    async def delete(self, key: ConnectionKey, *, actor: str, purge: bool = False) -> str:
        """Start removing a connection in the background and return the job id.

        Raises:
            HarborNotFoundError: No such connection in the tenant.
            HarborAlreadyRunningError: An ingestion of the connection is running.
        """
        control_plane = self._control_plane()
        connection = await self._require(key)
        active = await self._active_run(key.tenant_id, connection.source_key)
        if active is not None:
            raise HarborAlreadyRunningError(
                "an ingestion is running for this connection",
                details={"ingestion_id": active.task_id},
            )
        job = await self._start_job(connection, actor=actor, purge=purge)
        await control_plane.connections.set_status(
            tenant_id=key.tenant_id,
            source_key=connection.source_key,
            status="deleting",
            detail={"job_id": job.id, "purge": purge},
        )
        await self._record(connection, "deleted", actor, None, summary="Started removal")
        self.spawn(self.finish_deletion(connection, job_id=job.id, purge=purge))
        return job.id

    async def finish_deletion(self, connection: Connection, *, job_id: str, purge: bool) -> None:
        """Remove the documents, then the secrets, then the row; idempotent and resumable."""
        marker = (connection.tenant_id, connection.source_key)
        if marker in self._removing:
            return
        self._removing.add(marker)
        try:
            await self._finish_deletion(connection, job_id=job_id, purge=purge)
        finally:
            self._removing.discard(marker)

    async def _finish_deletion(self, connection: Connection, *, job_id: str, purge: bool) -> None:
        """Run one removal pass and record its outcome on the job.

        A connection removed as part of a project cascade leaves the project's
        job running; only the project removal finishes it.
        """
        control_plane = self._control_plane()
        job = await control_plane.jobs.get(job_id, tenant_ids=frozenset({connection.tenant_id}))
        try:
            await self._removal().remove(
                tenant_id=connection.tenant_id, source_key=connection.source_key, purge=purge
            )
            refs = set(stored_refs(connection.credentials))
            if control_plane.connection_revisions is not None:
                for snapshot in await control_plane.connection_revisions.list_for_source(
                    tenant_id=connection.tenant_id, source_key=connection.source_key
                ):
                    refs.update(ref for ref in snapshot.secret_refs if ref.startswith("secret://"))
            await retire_refs(control_plane, sorted(refs), tenant_id=connection.tenant_id)
            await control_plane.connections.delete(
                tenant_id=connection.tenant_id,
                project_id=connection.project_id,
                connection_id=connection.connection_id,
            )
        except Exception as error:
            logger.warning(
                "Connection removal failed; it will be retried job_id=%s error_type=%s",
                job_id,
                type(error).__name__,
            )
            if job is None:
                return
            if job.job_type == _DELETE_CONNECTION_JOB:
                await control_plane.jobs.save(_finished(job, "failed", type(error).__name__))
            else:
                await control_plane.jobs.save(replace(job, last_error=type(error).__name__))
            return
        if job is not None and job.job_type == _DELETE_CONNECTION_JOB:
            await control_plane.jobs.save(_finished(job, "succeeded"))

    async def resume_deletions(self) -> int:
        """Finish every removal a previous process left in ``deleting``."""
        connections, _ = await self._control_plane().connections.list(
            tenant_ids=None, status="deleting"
        )
        for connection in connections:
            detail = connection.status_detail or {}
            await self.finish_deletion(
                connection,
                job_id=str(detail.get("job_id", "")),
                purge=bool(detail.get("purge", False)),
            )
        return len(connections)

    async def _refuse_if_running(self, tenant_id: str, source_key: str) -> None:
        """Raise ``already_running`` naming the connection's active ingestion, if any."""
        active = await self._active_run(tenant_id, source_key)
        if active is not None:
            raise HarborAlreadyRunningError(
                "an ingestion is already running", details={"ingestion_id": active.task_id}
            )

    async def _active_run(self, tenant_id: str, source_key: str) -> IngestionTask | None:
        """The connection's pending, running or paused run; ones whose workflow ended are settled."""
        return await active_run(
            await self._runs(), self._ingestions, tenant_id=tenant_id, source_key=source_key
        )

    async def connection_busy(self, tenant_id: str, source_key: str) -> bool:
        """True while an ingestion of the connection is pending, running or paused."""
        return await self._active_run(tenant_id, source_key) is not None

    async def aclose(self) -> None:
        """Wait for background removals started by this process."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def _run_checks(  # noqa: PLR0913 - the parts of one connection test
        self,
        spec: SourceTypeSpec,
        validated: Any,
        effective: Mapping[str, Any],
        secret_values: Mapping[str, str],
        tenant_id: str,
    ) -> ConnectionTestReport:
        """Build the connector off the event loop and run the check chain under a deadline.

        Checks run on a small pool of their own, with no retries and short request
        timeouts, so a source that never answers cannot hold the API's shared
        threads after the deadline.
        """
        credentials = _merge_secrets(validated.credentials, effective, spec.secret_fields)
        tuning = _test_tuning(spec, validated.tuning)

        def check() -> ConnectionTestReport:
            try:
                connector = build_connector_from_parts(
                    spec.source_type,
                    tenant_id=tenant_id,
                    credentials=credentials,
                    config=validated.config,
                    tuning=tuning,
                    secret_values=secret_values,
                )
            except ConnectorConfigurationError as error:
                report = ConnectionTestReport()
                report.passed("schema")
                report.failed("reachable", "unreachable", str(error))
                return report
            return run_connection_checks(connector)

        try:
            running = asyncio.get_running_loop().run_in_executor(_TEST_EXECUTOR, check)
            return await asyncio.wait_for(running, TEST_TIMEOUT_SECONDS)
        except TimeoutError:
            return timed_out_report()

    async def _require(self, key: ConnectionKey) -> Connection:
        """Return the stored connection or raise not found."""
        connection = await self._control_plane().connections.get(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
        if connection is None:
            raise HarborNotFoundError(f"connection {key.connection_id!r} not found")
        return connection

    async def _require_project(self, key: ConnectionKey) -> None:
        """Raise not found unless the project exists in the tenant, naming a near match."""
        projects = self._control_plane().projects
        tenants = frozenset({key.tenant_id})
        if await projects.get(key.project_id, tenant_ids=tenants) is not None:
            return
        wanted = key.project_id.casefold()
        match = next(
            (
                project.id
                for project in await projects.list(tenant_ids=tenants)
                if wanted in {project.id.casefold(), project.name.casefold()}
            ),
            None,
        )
        hint = f"; did you mean {match!r}?" if match else ""
        raise HarborNotFoundError(f"project {key.project_id!r} not found{hint}")

    async def _view(self, connection: Connection) -> dict[str, Any]:
        """Return the API view with the latest run and document count."""
        views = await read_connection_views(await self._runs(), [connection], batch=False)
        return views[0]

    async def _retire_replaced(self, connection: Connection, refs: list[str]) -> None:
        """Delete replaced stored secrets now, or once the running ingestion finishes."""
        if not refs:
            return
        control_plane = self._control_plane()
        if await self.connection_busy(connection.tenant_id, connection.source_key):
            await defer_retirement(
                control_plane,
                refs,
                tenant_id=connection.tenant_id,
                source_key=connection.source_key,
            )
            return
        await retire_refs(control_plane, refs, tenant_id=connection.tenant_id)

    async def _record(  # noqa: PLR0913 - one audit entry
        self,
        connection: Connection,
        verb: str,
        actor: str,
        before: Connection | None,
        *,
        summary: str | None = None,
    ) -> None:
        """Append an audit entry naming the changed fields, never their values."""
        changed = _changed_fields(before, connection) if before is not None else []
        text = summary or (
            f"{verb.capitalize()} connection {connection.ref!r}"
            + (f"; changed {', '.join(changed)}" if changed else "")
        )
        await log_activity(
            self._control_plane(),
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=connection.tenant_id,
                actor=actor,
                verb=verb,
                entity_type="connection",
                entity_id=connection.ref,
                summary=text,
            ),
        )

    async def _start_job(self, connection: Connection, *, actor: str, purge: bool) -> Job:
        """Persist the removal job."""
        job = Job(
            id=f"job_{uuid4().hex[:16]}",
            source_id=connection.id,
            project_id=connection.project_id,
            job_type=_DELETE_CONNECTION_JOB,
            tenant_id=connection.tenant_id,
            status="running",
            payload={"connection": connection.ref, "purge": purge, "actor": actor},
        )
        return await self._control_plane().jobs.save(job)

    def spawn(self, work: Awaitable[None]) -> None:
        """Run ``work`` in the background and keep a reference until it ends."""
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def _check_preconditions(
    existing: Connection | None,
    spec: SourceTypeSpec,
    expected_version: int | None,
    create_only: bool,
) -> None:
    """Refuse a write that would break an immutable field or a precondition, before any I/O."""
    if existing is None:
        if expected_version is not None:
            raise HarborPreconditionFailedError("connection does not exist")
        return
    if existing.status == "deleting":
        raise ConnectionUnavailableError(f"connection {existing.ref!r} is being deleted")
    if existing.source_type != spec.source_type:
        raise HarborImmutableFieldError(
            "source_type cannot change; delete the connection or pick another connection_id",
            field="source_type",
        )
    if create_only:
        raise HarborPreconditionFailedError("connection already exists")
    if expected_version is not None and expected_version != existing.version:
        raise HarborPreconditionFailedError("connection version does not match")


def _plan_credentials(
    incoming: Mapping[str, Any], existing: Connection | None, spec: SourceTypeSpec
) -> tuple[dict[str, Any], frozenset[str]]:
    """Return the sent credentials without ``SecretStatus`` echoes, and the stored secrets kept.

    A secret left out, echoed as its status, or sent back as the reference it
    already holds keeps its stored value only when it belongs to the auth method
    being written and no endpoint field changed.

    Raises:
        ConnectionValidationError: An endpoint changed while a stored secret would be kept.
    """
    sent = {name: value for name, value in incoming.items() if not _is_status_echo(value)}
    if existing is None:
        return sent, frozenset()
    try:
        secrets = spec.method_secret_fields(sent.get(METHOD_KEY))
    except KeyError:
        return sent, frozenset()
    keep = frozenset(name for name in secrets if name not in sent and name in existing.credentials)
    reused = frozenset(
        name
        for name in secrets
        if name in sent and reuses_stored_reference(sent[name], existing.credentials.get(name))
    )
    moved = sorted(
        name
        for name in spec.endpoints
        if _endpoint(sent.get(name)) != _endpoint(existing.credentials.get(name))
    )
    if (keep or reused) and moved:
        raise ConnectionValidationError(
            [
                FieldIssue(f"credentials.{name}", f"send it again: {', '.join(moved)} changed")
                for name in sorted(keep | reused)
            ]
        )
    return sent, keep


class _ConcurrentWriteError(Exception):
    """A save without a precondition lost a race with another write."""


def _refuse_operator_fields(
    spec: SourceTypeSpec, write: ConnectionWrite, existing: Connection | None, origin: WriteOrigin
) -> None:
    """Refuse an API write that only operators may make, such as setting a worker path.

    Raises:
        ConnectionValidationError: ``origin`` is ``api`` and an operator-only field
            changes or an API-only install rule refuses the config.
    """
    if origin != "api":
        return
    issues = [
        *spec.operator_field_issues(write.credentials, existing.credentials if existing else None),
        *spec.api_policy_issues(write.config),
    ]
    if issues:
        raise ConnectionValidationError(issues)


def _test_tuning(spec: SourceTypeSpec, tuning: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``tuning`` for a connection test: no retries, no request past a short timeout."""
    capped = dict(tuning)
    if "max_retries" in spec.tuning_fields:
        capped["max_retries"] = 0
    if "request_timeout_seconds" in spec.tuning_fields:
        configured = capped.get("request_timeout_seconds")
        capped["request_timeout_seconds"] = min(
            configured or TEST_REQUEST_TIMEOUT_SECONDS, TEST_REQUEST_TIMEOUT_SECONDS
        )
    return capped


def _secret_sources(origin: WriteOrigin) -> frozenset[SecretSource]:
    """Reference sources a caller may set: ``env:`` only from operator paths."""
    return frozenset({"stored", "env"}) if origin == "operator" else frozenset({"stored"})


def _is_status_echo(value: Any) -> bool:
    """True for a ``SecretStatus`` sent back as read."""
    return isinstance(value, Mapping) and "set" in value and "secret_ref" not in value


def _endpoint(value: Any) -> str | None:
    """Normalize an endpoint URL for comparison; blank counts as unset."""
    text = str(value).strip().rstrip("/").casefold() if value is not None else ""
    return text or None


def _effective_credentials(
    sent: Mapping[str, Any], existing: Connection | None, keep: frozenset[str]
) -> dict[str, Any]:
    """Credentials as they will be once the write applies, for validation only."""
    kept = {name: existing.credentials[name] for name in keep} if existing else {}
    return {**kept, **{name: value for name, value in sent.items() if value is not None}}


def _validate(
    spec: SourceTypeSpec,
    credentials: Mapping[str, Any],
    write: ConnectionWrite,
    existing: Connection | None,
    tenant_id: str,
) -> ValidatedConnection:
    """Check the write against the source type's schemas, then against its connector.

    Raises:
        ConnectionValidationError: A schema or the connector rejects the values.
    """
    return validate_connection(
        spec, credentials, write.config, write.effective_tuning(existing), tenant_id=tenant_id
    )


def _build(  # noqa: PLR0913 - the parts of one stored connection
    key: ConnectionKey,
    existing: Connection | None,
    spec: SourceTypeSpec,
    write: ConnectionWrite,
    credentials: dict[str, Any],
    validated: ValidatedConnection,
    actor: str,
) -> Connection:
    """Assemble the connection to store; a write that may fix it clears an attention status."""
    builder = (
        ConnectionBuilder.from_existing(existing)
        if existing
        else ConnectionBuilder.for_new(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
    )
    builder.source_type(spec.source_type).name(write.name).credentials(credentials)
    builder.config(validated.config).tuning(validated.tuning)
    builder.secret_refs(all_refs(credentials)).actor(actor)
    if existing is not None and (
        existing.status == "error"
        or existing.credentials != credentials
        or (existing.status == "needs_reauth" and _sends_secret_value(write, spec))
    ):
        builder.status("active")
    return builder.build()


def _sends_secret_value(write: ConnectionWrite, spec: SourceTypeSpec) -> bool:
    """True when the write carries a secret as a value, not a status or reference."""
    return any(
        isinstance(write.credentials.get(name), str) and write.credentials.get(name)
        for name in spec.secret_fields
    )


def _unchanged(before: Connection, after: Connection) -> bool:
    """True when a write would store exactly what ``before`` holds."""
    return (
        before.name == after.name
        and before.status == after.status
        and before.status_detail == after.status_detail
        and not _changed_fields(before, after)
    )


def _changed_fields(before: Connection, after: Connection) -> list[str]:
    """Names of fields a write changed, as ``name`` or ``<part>.<field>``."""
    changed = ["name"] if before.name != after.name else []
    for part, label in (
        ("credentials", "credentials"),
        ("config", "config"),
        ("tuning", "advanced"),
    ):
        old: Mapping[str, Any] = getattr(before, part)
        new: Mapping[str, Any] = getattr(after, part)
        changed.extend(
            f"{label}.{name}"
            for name in sorted(set(old) | set(new))
            if old.get(name) != new.get(name)
        )
    return changed


def _merge_secrets(
    validated: Mapping[str, Any], secrets: Mapping[str, Any], secret_fields: frozenset[str]
) -> dict[str, Any]:
    """Validated non-secret fields plus the secret fields in the form ``secrets`` holds them."""
    merged = {name: value for name, value in validated.items() if name not in secret_fields}
    merged.update({name: value for name, value in secrets.items() if name in secret_fields})
    return merged


def _finished(job: Job, status: JobStatus, error: str | None = None) -> Job:
    """Return ``job`` marked finished with ``status``."""
    return replace(job, status=status, finished_at=utc_now(), last_error=error)
