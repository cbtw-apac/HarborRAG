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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import uuid4

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborCapabilityError,
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
from harborrag_core.security.secret_refs import all_refs, stored_refs
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.composition.connection_removal import ConnectionRemoval
from harborrag_runtime.config.connectors.models import SourceTypeSpec, source_types
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.connections import (
    CredentialSecretWriter,
    SecretUnavailableError,
    default_secret_resolver,
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
from .connection_views import connection_view, ingestion_view
from .effect_recovery import defer_retirement, log_activity, retire_refs

logger = logging.getLogger("harborrag.app.workflow_control.control_plane.connections")

TEST_TIMEOUT_SECONDS = 15.0
SYNC_LEASE_TTL_SECONDS = 5.0


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


class IngestionSubmitter(Protocol):
    """Start an ingestion through the shared submission path."""

    async def submit(
        self, command: IngestionCreateCommand, *, idempotency_key: str | None
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class ConnectionWrite:
    """The body of a connection ``PUT`` or ``test``."""

    source_type: str
    credentials: Mapping[str, Any]
    config: Mapping[str, Any]
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionPutResult:
    """A stored connection, whether it was new, and the ingestion ``sync_now`` started."""

    connection: dict[str, Any]
    created: bool
    version: int
    ingestion: dict[str, Any] | None = None


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
        return [await self._view(connection) for connection in connections], next_cursor

    async def catalog(self, *, tenant_ids: frozenset[str] | None) -> list[dict[str, str]]:
        """Connection identities ``POST /v1/ingestions`` accepts, by ``source_key``."""
        connections, _ = await self._control_plane().connections.list(tenant_ids=tenant_ids)
        return sorted(
            (
                {"connection_id": connection.source_key, "source_type": connection.source_type}
                for connection in connections
                if connection.status != "deleting"
            ),
            key=lambda item: item["connection_id"],
        )

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
                "ref": connection.ref,
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
    ) -> ConnectionPutResult:
        """Create or overwrite a connection.

        ``config`` is replaced whole. In ``credentials`` an omitted secret keeps
        its value, a value rotates it, a ``{"secret_ref"}`` points it elsewhere
        and ``null`` clears it; replaced stored secrets are deleted once no
        running ingestion still uses them.

        Args:
            key: The connection's tenant, project and ``connection_id``.
            write: Source type, name, credentials and config as sent.
            actor: Subject of the caller's token.
            expected_version: ``If-Match`` version, when sent.
            create_only: ``If-None-Match: *`` was sent.
            sync_now: Start an incremental sync after saving.

        Returns:
            The stored connection, whether it was created, and the started ingestion.

        Raises:
            HarborNotFoundError: The project does not exist in the tenant.
            ConnectionValidationError: The type is unknown or a field fails its schema.
            HarborImmutableFieldError: The connection exists with another source type.
            HarborPreconditionFailedError: ``If-Match`` or ``If-None-Match`` does not hold.
            ConnectionUnavailableError: The connection is being deleted.
        """
        control_plane = self._control_plane()
        await self._require_project(key)
        require_connection_id(key.connection_id)
        spec = source_types.get(write.source_type)
        existing = await control_plane.connections.get(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
        _check_preconditions(existing, spec, expected_version, create_only)
        effective = _effective_credentials(write.credentials, existing, spec.secret_fields)
        validated = spec.validate(effective, write.config, existing.tuning if existing else None)
        written = await CredentialSecretWriter(
            control_plane.secrets, tenant_id=key.tenant_id
        ).apply(
            write.credentials,
            secret_fields=spec.secret_fields,
            existing=existing.credentials if existing else None,
        )
        credentials = _merge_secrets(validated.credentials, written.credentials, spec.secret_fields)
        connection = _build(key, existing, spec, write, credentials, validated.config, actor)
        try:
            stored, outcome = await control_plane.connections.put(
                connection, expected_version=expected_version, create_only=create_only
            )
        except Exception:
            await retire_refs(control_plane, written.created, tenant_id=key.tenant_id)
            raise
        await self._retire_replaced(stored, written.retired)
        await self._record(
            stored, "created" if outcome == "created" else "updated", actor, existing
        )
        ingestion = await self.sync(key, actor=actor) if sync_now else None
        return ConnectionPutResult(
            connection=await self._view(stored),
            created=outcome == "created",
            version=stored.version,
            ingestion=ingestion,
        )

    async def test(self, key: ConnectionKey, write: ConnectionWrite | None) -> dict[str, Any]:
        """Check form values, or the saved connection when ``write`` is None; saves nothing.

        Returns:
            The ``ConnectionTest`` body; ``ok`` is False when a check failed.

        Raises:
            HarborNotFoundError: No body and no saved connection, or the project is missing.
            ConnectionValidationError: The values fail the source type's schema.
        """
        control_plane = self._control_plane()
        existing = await control_plane.connections.get(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
        if write is None:
            if existing is None:
                raise HarborNotFoundError(f"connection {key.connection_id!r} not found")
            write = ConnectionWrite(existing.source_type, existing.credentials, existing.config)
        else:
            await self._require_project(key)
        spec = source_types.get(write.source_type)
        effective = _effective_credentials(write.credentials, existing, spec.secret_fields)
        validated = spec.validate(effective, write.config, existing.tuning if existing else None)
        try:
            secret_values = await default_secret_resolver(
                control_plane.secrets
            ).resolve_credentials(
                {k: v for k, v in effective.items() if isinstance(v, Mapping)},
                tenant_id=key.tenant_id,
            )
        except SecretUnavailableError:
            report = ConnectionTestReport()
            report.passed("schema")
            report.failed("auth", "auth_invalid", "a referenced secret cannot be read")
            return report.as_dict()
        return (await self._run_checks(spec, validated, effective, secret_values)).as_dict()

    async def sync(
        self,
        key: ConnectionKey,
        *,
        actor: str,
        mode: str = "incremental",
        item_refs: tuple[str, ...] = (),
        updated_after: str | None = None,
    ) -> dict[str, Any]:
        """Start an ingestion of the connection.

        Returns:
            The spec's ``Ingestion`` object of the started run.

        Raises:
            HarborNotFoundError: No such connection in the tenant.
            HarborAlreadyRunningError: An ingestion is active; ``details.ingestion_id`` names it.
            HarborCapabilityError: ``item_refs`` was sent; per-item syncs are not supported yet.
            ConnectionUnavailableError: The connection is being deleted.
        """
        if item_refs:
            raise HarborCapabilityError("syncing selected items is not supported yet")
        connection = await self._require(key)
        if connection.status == "deleting":
            raise ConnectionUnavailableError(f"connection {connection.ref!r} is being deleted")
        runs = await self._runs()
        await self._refuse_if_running(runs, key.tenant_id, connection.source_key)
        lease = f"connection-sync:{key.tenant_id}:{connection.source_key}"
        holder = f"{self._instance_id}:{uuid4().hex}"
        if not await self._control_plane().leases.try_acquire(
            lease, holder, ttl_seconds=SYNC_LEASE_TTL_SECONDS
        ):
            await self._refuse_if_running(runs, key.tenant_id, connection.source_key)
            raise HarborAlreadyRunningError(
                "an ingestion is starting for this connection", details={"ingestion_id": None}
            )
        await self._refuse_if_running(runs, key.tenant_id, connection.source_key)
        accepted = await self._ingestions.submit(
            IngestionCreateCommand(
                tenant_id=key.tenant_id,
                connection_id=connection.source_key,
                force_reprocess=mode == "full",
                public_request={
                    "connection": connection.ref,
                    "mode": mode,
                    "updated_after": updated_after,
                    "actor": actor,
                },
                updated_after=updated_after,
            ),
            idempotency_key=None,
        )
        task = await runs.get(str(accepted["task_id"]))
        if task is None:
            raise HarborUnavailableError("the ingestion was accepted but cannot be read back")
        await self._record(connection, "synced", actor, None, summary=f"Started a {mode} sync")
        return ingestion_view(
            task,
            connection_ref=connection.ref,
            progress=await runs.progress(task.task_id),
            triggered_by=actor,
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
        active = await (await self._runs()).active_for_connection(
            tenant_id=key.tenant_id, connection_id=connection.source_key
        )
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
        """Run one removal pass and record its outcome on the job."""
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
            if job is not None:
                await control_plane.jobs.save(_finished(job, "failed", type(error).__name__))
            return
        if job is not None:
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

    @staticmethod
    async def _refuse_if_running(runs: ConnectionRuns, tenant_id: str, source_key: str) -> None:
        """Raise ``already_running`` naming the connection's active ingestion, if any."""
        active = await runs.active_for_connection(tenant_id=tenant_id, connection_id=source_key)
        if active is not None:
            raise HarborAlreadyRunningError(
                "an ingestion is already running", details={"ingestion_id": active.task_id}
            )

    async def connection_busy(self, tenant_id: str, source_key: str) -> bool:
        """True while an ingestion of the connection is pending, running or paused."""
        runs = await self._runs()
        return (
            await runs.active_for_connection(tenant_id=tenant_id, connection_id=source_key)
            is not None
        )

    async def aclose(self) -> None:
        """Wait for background removals started by this process."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def _run_checks(
        self,
        spec: SourceTypeSpec,
        validated: Any,
        effective: Mapping[str, Any],
        secret_values: Mapping[str, str],
    ) -> ConnectionTestReport:
        """Build the connector off the event loop and run the check chain under a deadline."""
        credentials = _merge_secrets(validated.credentials, effective, spec.secret_fields)

        def check() -> ConnectionTestReport:
            try:
                connector = build_connector_from_parts(
                    spec.source_type,
                    credentials=credentials,
                    config=validated.config,
                    tuning=validated.tuning,
                    secret_values=secret_values,
                )
            except ConnectorConfigurationError:
                report = ConnectionTestReport()
                report.passed("schema")
                report.failed("reachable", "unreachable", "the source rejected these settings")
                return report
            return run_connection_checks(connector)

        try:
            return await asyncio.wait_for(asyncio.to_thread(check), TEST_TIMEOUT_SECONDS)
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
        """Raise not found unless the project exists in the tenant."""
        project = await self._control_plane().projects.get(
            key.project_id, tenant_ids=frozenset({key.tenant_id})
        )
        if project is None:
            raise HarborNotFoundError(f"project {key.project_id!r} not found")

    async def _view(self, connection: Connection) -> dict[str, Any]:
        """Return the API view with the latest run and document count."""
        runs = await self._runs()
        latest = await runs.latest_for_connection(
            tenant_id=connection.tenant_id, connection_id=connection.source_key
        )
        documents = await runs.connection_document_count(
            tenant_id=connection.tenant_id, connection_id=connection.source_key
        )
        return connection_view(connection, last_ingestion=latest, documents=documents)

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
            job_type="delete_connection",
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
            "source_type cannot change; delete the connection or pick another connection_id"
        )
    if create_only:
        raise HarborPreconditionFailedError("connection already exists")
    if expected_version is not None and expected_version != existing.version:
        raise HarborPreconditionFailedError("connection version does not match")


def _effective_credentials(
    incoming: Mapping[str, Any], existing: Connection | None, secret_fields: frozenset[str]
) -> dict[str, Any]:
    """Credentials as they will be once the write applies, for validation only."""
    kept = {
        name: value
        for name, value in (existing.credentials if existing else {}).items()
        if name in secret_fields and name not in incoming
    }
    sent = {
        name: _secret_echo(value, existing, name) if name in secret_fields else value
        for name, value in incoming.items()
        if value is not None
    }
    return {**kept, **sent}


def _secret_echo(value: Any, existing: Connection | None, name: str) -> Any:
    """Treat an echoed ``SecretStatus`` as the stored value it describes."""
    if isinstance(value, Mapping) and "set" in value and "secret_ref" not in value:
        return (existing.credentials if existing else {}).get(name, value)
    return value


def _build(  # noqa: PLR0913 - the parts of one stored connection
    key: ConnectionKey,
    existing: Connection | None,
    spec: SourceTypeSpec,
    write: ConnectionWrite,
    credentials: dict[str, Any],
    config: dict[str, Any],
    actor: str,
) -> Connection:
    """Assemble the connection to store; a write that fixes it clears an error status."""
    builder = (
        ConnectionBuilder.from_existing(existing)
        if existing
        else ConnectionBuilder.for_new(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
    )
    builder.source_type(spec.source_type).name(write.name).credentials(credentials).config(config)
    builder.secret_refs(all_refs(credentials)).actor(actor)
    if existing is not None and (existing.status == "error" or existing.credentials != credentials):
        builder.status("active")
    return builder.build()


def _changed_fields(before: Connection, after: Connection) -> list[str]:
    """Names of fields a write changed, ``credentials.<name>`` and ``config.<name>`` included."""
    changed = ["name"] if before.name != after.name else []
    for part in ("credentials", "config"):
        old: Mapping[str, Any] = getattr(before, part)
        new: Mapping[str, Any] = getattr(after, part)
        changed.extend(
            f"{part}.{name}"
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
