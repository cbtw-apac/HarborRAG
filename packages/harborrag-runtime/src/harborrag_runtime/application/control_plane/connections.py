"""Connection use cases: read, write, test, sync and delete one configured data source.

``put`` replaces the whole config (secret keep, rotate or clear; ETag preconditions), ``test``
saves nothing, ``sync`` allows one active ingestion per connection, and ``delete`` removes the
connection from every store in the background, resumed after a restart. Does not speak HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from harborrag_adapters.connectors.policies.network import DestinationPolicy
from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborCapabilityError,
    HarborConflictError,
    HarborError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
    HarborUnavailableError,
)
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.connection import Connection, ConnectionStatus, require_connection_id
from harborrag_core.domain.job import Job, JobStatus
from harborrag_core.ingestion import IngestionTask
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_core.security.secret_refs import stored_refs
from harborrag_engine.control_plane.connection_views import (
    ingestion_view,
    read_connection_views,
    resolve_any_version,
)
from harborrag_engine.ingestion.models import IngestionCreateCommand
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.models import (
    ConnectionValidationError,
    FieldIssue,
    source_types,
)
from harborrag_runtime.config.connectors.models.builder import validate_connection
from harborrag_runtime.connections import (
    CredentialSecretWriter,
    SecretUnavailableError,
    default_secret_resolver,
    reference_issues,
)
from harborrag_runtime.ingestion.connection_checks import ConnectionTestReport
from harborrag_runtime.ingestion.connections import ConnectionUnavailableError

from ..retrieval.access import corpus_mode_for
from .active_runs import active_run
from .connection_models import (
    ConnectionKey,
    ConnectionPutResult,
    ConnectionRuns,
    ConnectionWrite,
    IngestionSubmitter,
    WriteOrigin,
)
from .connection_testing import run_checks
from .connection_writes import (
    ConcurrentWriteError,
    build_connection,
    changed_fields,
    check_preconditions,
    effective_credentials,
    merge_secrets,
    plan_credentials,
    refuse_operator_fields,
    secret_sources,
    unchanged,
    validate_write,
)
from .effect_recovery import defer_retirement, log_activity, retire_refs

if TYPE_CHECKING:
    from harborrag_runtime.composition.connection_removal import ConnectionRemoval
    from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.application.control_plane.connections")

_DELETE_CONNECTION_JOB: Final = "delete_connection"
SYNC_LEASE_TTL_SECONDS = 5.0


class ConnectionService:
    """Facade over the connection repository, secrets, ingestion runs and removal.

    Args:
        control_plane: Returns the control-plane repositories.
        runs: Returns the run registry of connections, which also answers source ACLs.
        ingestions: Starts ingestions and settles runs that ended unrecorded.
        removal: Returns the stores a connection removal clears.
        destination_policy: Hosts a connection test may reach beyond public addresses.
        instance_id: ID of this process, which names the holder of a connection sync lease.
        settings: Settings that choose each tenant's corpus mode.
        schedule_removal: Removes a connection's schedules before its data; ``None``
            when nothing schedules connections.
    """

    def __init__(  # noqa: PLR0913 - the stores and settings the service composes
        self,
        *,
        control_plane: Callable[[], ControlPlaneRepositories],
        runs: Callable[[], Awaitable[ConnectionRuns]],
        ingestions: IngestionSubmitter,
        removal: Callable[[], ConnectionRemoval],
        destination_policy: DestinationPolicy,
        instance_id: str,
        settings: RuntimeSettings,
        schedule_removal: Callable[[ConnectionKey], Awaitable[None]] | None = None,
    ) -> None:
        self._control_plane = control_plane
        self._runs = runs
        self._ingestions = ingestions
        self._removal = removal
        self._destination_policy = destination_policy
        self._instance_id = instance_id
        self._settings = settings
        self._schedule_removal = schedule_removal
        self._background: set[asyncio.Task[None]] = set()
        self._removing: set[tuple[str, str]] = set()

    async def get(self, key: ConnectionKey) -> tuple[dict[str, Any], int]:
        """Return a connection's view and version.

        Raises:
            ConnectionValidationError: ``connection_id`` is malformed.
            HarborNotFoundError: No such connection in the tenant.
        """
        _require_connection_id(key)
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

    async def searchable(
        self, *, tenant_id: str, project_id: str, principal_id: str
    ) -> list[dict[str, Any]]:
        """List the connections of one project the reader can search, as filter chips.

        Under ``source_acl`` a connection is listed only when the reader may read one of
        its source scopes, so one never ingested is not listed. ``tenant_shared`` lists
        every connection.

        Args:
            tenant_id: Tenant of the project.
            project_id: Project whose connections to list.
            principal_id: Subject the source ACL checks, such as an API key's owner.

        Raises:
            HarborNotFoundError: The project does not exist in the tenant.
            HarborConflictError: The reader may read more source scopes than the
                permission store enumerates.
        """
        control_plane = self._control_plane()
        tenants = frozenset({tenant_id})
        if await control_plane.projects.get(project_id, tenant_ids=tenants) is None:
            raise HarborNotFoundError(f"project {project_id!r} not found")
        connections, _ = await control_plane.connections.list(
            tenant_ids=tenants, project_id=project_id
        )
        runs = await self._runs()
        live = [connection for connection in connections if connection.status != "deleting"]
        readable = await self._readable(runs, tenant_id, principal_id, live)
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
            for connection in readable
        ]

    async def _readable(
        self,
        runs: ConnectionRuns,
        tenant_id: str,
        principal_id: str,
        connections: list[Connection],
    ) -> list[Connection]:
        """Keep the connections with a source scope the reader may read; all under ``tenant_shared``."""
        access = AccessContext(
            principal_id=principal_id,
            tenant_id=TenantId(tenant_id),
            corpus_mode=corpus_mode_for(self._settings, tenant_id),
        )
        if access.corpus_mode == "tenant_shared" or not connections:
            return connections
        allowed = set(await runs.allowed_source_scope_ids(tenant_id, access=access))
        scopes = await runs.source_scope_ids_by_connection(
            tenant_id=tenant_id,
            connection_ids=[connection.source_key for connection in connections],
        )
        return [
            connection
            for connection in connections
            if allowed.intersection(scopes.get(connection.source_key, ()))
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
            ConnectionValidationError: ``connection_id`` is malformed, the type is unknown,
                a field fails its schema, an endpoint changed while a stored secret would
                be kept, or an ``api`` write sets a new ``env:`` reference or changes an
                operator-only field.
            HarborImmutableFieldError: The connection exists with another source type.
            HarborPreconditionFailedError: ``If-Match`` or ``If-None-Match`` does not hold.
            HarborConflictError: Without either, another write landed twice in a row
                between reading the connection and saving it.
            ConnectionUnavailableError: The connection is being deleted.
        """
        _require_connection_id(key)
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
            except ConcurrentWriteError:
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
            ConcurrentWriteError: ``guarded`` and another write landed first.
        """
        control_plane = self._control_plane()
        await self._require_project(key)
        spec = source_types.get(write.source_type)
        existing = await control_plane.connections.get(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
        expected_version = resolve_any_version(
            expected_version, existing.version if existing else None
        )
        check_preconditions(existing, spec, expected_version, create_only)
        refuse_operator_fields(spec, write, existing, origin)
        sent, keep = plan_credentials(write.credentials, existing, spec)
        validated = validate_write(
            spec, effective_credentials(sent, existing, keep), write, existing, key.tenant_id
        )
        written = await CredentialSecretWriter(
            control_plane.secrets, tenant_id=key.tenant_id, sources=secret_sources(origin)
        ).apply(
            sent,
            secret_fields=spec.secret_fields,
            existing=existing.credentials if existing else None,
            keep=keep,
        )
        credentials = merge_secrets(validated.credentials, written.credentials, spec.secret_fields)
        connection = build_connection(key, existing, spec, write, credentials, validated, actor)
        if existing is not None and not written.created and unchanged(existing, connection):
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
                raise ConcurrentWriteError from error
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
        self, key: ConnectionKey, stored: Connection, actor: str, *, created: bool, sync_now: bool
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
            ConnectionValidationError: ``connection_id`` is malformed, or the values fail
                the source type's schema, change an endpoint while a stored secret would be
                kept, name a stored secret the field does not hold, or, from ``api``, set a
                new ``env:`` reference or change an operator-only field.
        """
        _require_connection_id(key)
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
        sent, keep = plan_credentials(write.credentials, existing, spec)
        effective = effective_credentials(sent, existing, keep)
        refuse_operator_fields(spec, write, existing, origin)
        issues = reference_issues(
            sent, existing.credentials if existing else None, sources=secret_sources(origin)
        )
        if issues:
            raise ConnectionValidationError(issues)
        validated = validate_write(spec, effective, write, existing, key.tenant_id)
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
        report = await run_checks(
            spec,
            validated,
            effective,
            secret_values,
            key.tenant_id,
            self._destination_policy,
        )
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
        await self.refuse_if_running(key.tenant_id, connection.source_key)
        lease = f"connection-sync:{key.tenant_id}:{connection.source_key}"
        holder = f"{self._instance_id}:{uuid4().hex}"
        if not await self._control_plane().leases.try_acquire(
            lease, holder, ttl_seconds=SYNC_LEASE_TTL_SECONDS
        ):
            await self.refuse_if_running(key.tenant_id, connection.source_key)
            raise HarborAlreadyRunningError(
                "an ingestion is starting for this connection", details={"ingestion_id": None}
            )
        try:
            await self.refuse_if_running(key.tenant_id, connection.source_key)
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
        finally:
            await self._control_plane().leases.release(lease, holder)
        task = await runs.get(str(accepted["task_id"]))
        if task is None:
            raise HarborUnavailableError("the ingestion was accepted but cannot be read back")
        await self._record(connection, "synced", actor, None, summary=f"Started a {mode} sync")
        return ingestion_view(
            task, connection=connection, progress=await runs.progress(task.task_id), mode=mode
        )

    async def delete(self, key: ConnectionKey, *, actor: str, purge: bool = False) -> str:
        """Start removing a connection in the background and return the job id.

        Raises:
            ConnectionValidationError: ``connection_id`` is malformed.
            HarborNotFoundError: No such connection in the tenant.
            HarborAlreadyRunningError: An ingestion of the connection is running.
        """
        _require_connection_id(key)
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
        """Remove the schedules, documents, secrets, then the row; idempotent and resumable."""
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
            await self._remove_schedules(connection)
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

    async def _remove_schedules(self, connection: Connection) -> None:
        """Remove the connection's schedules first, so no slot starts a run during removal."""
        if self._schedule_removal is not None:
            await self._schedule_removal(
                ConnectionKey(connection.tenant_id, connection.project_id, connection.connection_id)
            )

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

    async def refuse_if_running(self, tenant_id: str, source_key: str) -> None:
        """Refuse a new ingestion of a connection that has a pending, running or paused one.

        Raises:
            HarborAlreadyRunningError: An ingestion is active; ``details.ingestion_id`` names it.
        """
        active = await self._active_run(tenant_id, source_key)
        if active is not None:
            raise HarborAlreadyRunningError(
                "an ingestion is already running", details={"ingestion_id": active.task_id}
            )

    async def _active_run(self, tenant_id: str, source_key: str) -> IngestionTask | None:
        """Return the connection's pending, running or paused run, settling ended ones first."""
        return await active_run(
            await self._runs(), self._ingestions, tenant_id=tenant_id, source_key=source_key
        )

    async def connection_busy(self, tenant_id: str, source_key: str) -> bool:
        """Report whether an ingestion of the connection is pending, running or paused."""
        return await self._active_run(tenant_id, source_key) is not None

    async def aclose(self) -> None:
        """Wait for background removals started by this process."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

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
        changed = changed_fields(before, connection) if before is not None else []
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


def _require_connection_id(key: ConnectionKey) -> None:
    """Refuse a malformed ``connection_id`` before any lookup, as the HTTP path pattern does.

    Raises:
        ConnectionValidationError: The id does not match the ``connection_id`` pattern.
    """
    try:
        require_connection_id(key.connection_id)
    except ValueError as error:
        raise ConnectionValidationError([FieldIssue("connection_id", str(error))]) from error


def _finished(job: Job, status: JobStatus, error: str | None = None) -> Job:
    """Return ``job`` marked finished with ``status``."""
    return replace(job, status=status, finished_at=utc_now(), last_error=error)
