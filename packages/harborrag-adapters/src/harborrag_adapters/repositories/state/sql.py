from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from harborrag_core.schemas.ids import WorkflowId
from harborrag_core.schemas.state import CheckpointRecord, LeaseRecord, WorkflowState
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)
from sqlalchemy import JSON, Boolean, Column, Integer, MetaData, String, Table, UniqueConstraint
from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
    HarborStorageLeaseError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.shared.sqlalchemy import SQLAlchemyDBClient, UTCDateTime
from harborrag_adapters.repositories.shared.tenancy import ensure_tenant
from harborrag_adapters.repositories.state.base import (
    HarborCheckpointStore,
    HarborLeaseStore,
    HarborStateBackend,
    HarborStateStore,
)
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)

_METADATA = MetaData()

_WORKFLOW_STATE = Table(
    "harbor_workflow_state",
    _METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("workflow_id", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("current_step", String(255), nullable=True),
    Column("payload", JSON, nullable=False, default=dict),
    Column("cursor", JSON, nullable=False, default=dict),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("version", Integer, nullable=False, default=1),
    Column("cancellation_requested", Boolean, nullable=False, default=False),
    Column("error", JSON, nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("expires_at", UTCDateTime(), nullable=True),
)

_CHECKPOINTS = Table(
    "harbor_checkpoints",
    _METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False, index=True),
    Column("workflow_id", String(64), nullable=False, index=True),
    Column("step_name", String(255), nullable=False),
    Column("cursor", JSON, nullable=False, default=dict),
    Column("payload", JSON, nullable=False, default=dict),
    Column("state_version", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("parent_checkpoint_id", String(64), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    UniqueConstraint(
        "tenant_id",
        "workflow_id",
        "state_version",
        name="uq_harbor_checkpoint_stream_version",
    ),
)

_LEASES = Table(
    "harbor_leases",
    _METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("resource", String(255), primary_key=True),
    Column("owner_token", String(64), nullable=False),
    Column("fencing_token", Integer, nullable=False),
    Column("acquired_at", UTCDateTime(), nullable=False),
    Column("expires_at", UTCDateTime(), nullable=False),
)

_LEASE_FENCING = Table(
    "harbor_lease_fencing",
    _METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("resource", String(255), primary_key=True),
    Column("counter", Integer, nullable=False, default=0),
)


def _state_from_row(row: Any) -> WorkflowState:
    return WorkflowState.model_validate(
        {
            "workflow_id": row["workflow_id"],
            "tenant_id": row["tenant_id"],
            "status": row["status"],
            "current_step": row["current_step"],
            "payload": dict(row["payload"] or {}),
            "cursor": dict(row["cursor"] or {}),
            "retry_count": row["retry_count"],
            "version": row["version"],
            "cancellation_requested": row["cancellation_requested"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
        }
    )


def _checkpoint_from_row(row: Any) -> CheckpointRecord:
    return CheckpointRecord.model_validate(
        {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "tenant_id": row["tenant_id"],
            "step_name": row["step_name"],
            "cursor": dict(row["cursor"] or {}),
            "payload": dict(row["payload"] or {}),
            "state_version": row["state_version"],
            "status": row["status"],
            "parent_checkpoint_id": row["parent_checkpoint_id"],
            "created_at": row["created_at"],
        }
    )


class SQLStoreBase:
    """Holds the shared SQL backend used by cohesive state products."""

    def __init__(self, backend: SQLStateBackend) -> None:
        self._backend = backend
        self._telemetry = backend._telemetry

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        async with self._backend.client.sessions() as session, session.begin():
            yield session


class SQLStateStore(SQLStoreBase, HarborStateStore):
    """Persists versioned workflow state using SQL optimistic concurrency."""

    @traced_repository_operation("state_create")
    async def create(
        self,
        state: WorkflowState,
        *,
        context: StorageOperationContext,
    ) -> WorkflowState:
        ensure_tenant(
            state.tenant_id,
            context,
            error_context=self._backend.error_context(
                "state_create", context, str(state.workflow_id)
            ),
        )
        try:
            async with self._session() as session:
                await session.execute(
                    sa_insert(_WORKFLOW_STATE).values(
                        tenant_id=str(context.tenant_id),
                        workflow_id=str(state.workflow_id),
                        status=state.status.value,
                        current_step=state.current_step,
                        payload=state.payload,
                        cursor=state.cursor,
                        retry_count=state.retry_count,
                        version=state.version,
                        cancellation_requested=state.cancellation_requested,
                        error=state.error,
                        created_at=state.created_at,
                        updated_at=state.updated_at,
                        expires_at=state.expires_at,
                    )
                )
        except IntegrityError as exc:
            raise HarborStorageAlreadyExistsError(
                f"workflow state {state.workflow_id!r} already exists",
                context=self._backend.error_context(
                    "state_create", context, str(state.workflow_id)
                ),
            ) from exc
        return state

    @traced_repository_operation("state_get")
    async def get(
        self,
        workflow_id: WorkflowId,
        *,
        context: StorageOperationContext,
    ) -> WorkflowState | None:
        async with self._session() as session:
            row = (
                (
                    await session.execute(
                        sa_select(_WORKFLOW_STATE).where(
                            _WORKFLOW_STATE.c.tenant_id == str(context.tenant_id),
                            _WORKFLOW_STATE.c.workflow_id == str(workflow_id),
                            (
                                _WORKFLOW_STATE.c.expires_at.is_(None)
                                | (_WORKFLOW_STATE.c.expires_at > datetime.now(UTC))
                            ),
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _state_from_row(row) if row else None

    @traced_repository_operation("state_save")
    async def save(
        self,
        state: WorkflowState,
        *,
        expected_version: int,
        context: StorageOperationContext,
    ) -> WorkflowState:
        ensure_tenant(
            state.tenant_id,
            context,
            error_context=self._backend.error_context(
                "state_save", context, str(state.workflow_id)
            ),
        )
        now = datetime.now(UTC)
        async with self._session() as session:
            result = await session.execute(
                sa_update(_WORKFLOW_STATE)
                .where(
                    _WORKFLOW_STATE.c.tenant_id == str(context.tenant_id),
                    _WORKFLOW_STATE.c.workflow_id == str(state.workflow_id),
                    _WORKFLOW_STATE.c.version == expected_version,
                )
                .values(
                    status=state.status.value,
                    current_step=state.current_step,
                    payload=state.payload,
                    cursor=state.cursor,
                    retry_count=state.retry_count,
                    version=expected_version + 1,
                    cancellation_requested=state.cancellation_requested,
                    error=state.error,
                    updated_at=now,
                    expires_at=state.expires_at,
                )
            )
            if result.rowcount != 1:
                raise self._conflict(context, state.workflow_id, expected_version)
        return state.model_copy(update={"version": expected_version + 1, "updated_at": now})

    def _conflict(
        self,
        context: StorageOperationContext,
        workflow_id: WorkflowId,
        expected: int,
    ) -> HarborStorageCheckpointConflictError:
        error_context = self._backend.error_context("state_save", context, str(workflow_id))
        error_context.metadata = {"expected_version": expected}
        return HarborStorageCheckpointConflictError(
            f"workflow {workflow_id!r} version conflict",
            context=error_context,
        )


class SQLCheckpointStore(SQLStoreBase, HarborCheckpointStore):
    """Persists append-only checkpoints with optimistic concurrency."""

    @traced_repository_operation("checkpoint_load_latest")
    async def load_latest(
        self,
        workflow_id: WorkflowId,
        *,
        context: StorageOperationContext,
    ) -> CheckpointRecord | None:
        async with self._session() as session:
            row = (
                (
                    await session.execute(
                        sa_select(_CHECKPOINTS)
                        .where(
                            _CHECKPOINTS.c.tenant_id == str(context.tenant_id),
                            _CHECKPOINTS.c.workflow_id == str(workflow_id),
                        )
                        .order_by(_CHECKPOINTS.c.state_version.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        return _checkpoint_from_row(row) if row else None

    @traced_repository_operation("checkpoint_save")
    async def save(
        self,
        checkpoint: CheckpointRecord,
        *,
        expected_version: int | None,
        context: StorageOperationContext,
    ) -> CheckpointRecord:
        ensure_tenant(
            checkpoint.tenant_id,
            context,
            error_context=self._backend.error_context(
                "checkpoint_save", context, str(checkpoint.workflow_id)
            ),
        )
        try:
            async with self._session() as session:
                actual = (
                    await session.execute(
                        sa_select(_CHECKPOINTS.c.state_version)
                        .where(
                            _CHECKPOINTS.c.tenant_id == str(context.tenant_id),
                            _CHECKPOINTS.c.workflow_id == str(checkpoint.workflow_id),
                        )
                        .order_by(_CHECKPOINTS.c.state_version.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                required = 1 if actual is None else actual + 1
                if actual != expected_version or checkpoint.state_version != required:
                    raise self._conflict(context, checkpoint.workflow_id, expected_version, actual)
                await session.execute(
                    sa_insert(_CHECKPOINTS).values(
                        id=str(checkpoint.id),
                        tenant_id=str(context.tenant_id),
                        workflow_id=str(checkpoint.workflow_id),
                        step_name=checkpoint.step_name,
                        cursor=checkpoint.cursor,
                        payload=checkpoint.payload,
                        state_version=checkpoint.state_version,
                        status=checkpoint.status.value,
                        parent_checkpoint_id=(
                            str(checkpoint.parent_checkpoint_id)
                            if checkpoint.parent_checkpoint_id
                            else None
                        ),
                        created_at=checkpoint.created_at,
                    )
                )
        except IntegrityError as exc:
            raise self._conflict(context, checkpoint.workflow_id, expected_version, None) from exc
        return checkpoint

    def _conflict(
        self,
        context: StorageOperationContext,
        workflow_id: WorkflowId,
        expected: int | None,
        actual: int | None,
    ) -> HarborStorageCheckpointConflictError:
        error_context = self._backend.error_context("checkpoint_save", context, str(workflow_id))
        error_context.metadata = {"expected_version": expected, "actual_version": actual}
        return HarborStorageCheckpointConflictError(
            f"checkpoint stream {workflow_id!r} version conflict",
            context=error_context,
        )


class SQLLeaseStore(SQLStoreBase, HarborLeaseStore):
    """Coordinates expiring leases with monotonic fencing tokens."""

    @traced_repository_operation("lease_acquire")
    async def acquire(
        self,
        resource: str,
        owner_token: str,
        lease_duration: timedelta,
        *,
        context: StorageOperationContext,
    ) -> LeaseRecord | None:
        self._validate_duration(lease_duration)
        now = datetime.now(UTC)
        expires_at = now + lease_duration
        tenant_id = str(context.tenant_id)
        try:
            async with self._session() as session:
                # Updating the durable counter first serializes contenders for an
                # existing resource. The counter increment and lease claim share
                # one transaction, so a losing claim rolls the increment back.
                counter_update = await session.execute(
                    sa_update(_LEASE_FENCING)
                    .where(
                        _LEASE_FENCING.c.tenant_id == tenant_id,
                        _LEASE_FENCING.c.resource == resource,
                    )
                    .values(counter=_LEASE_FENCING.c.counter + 1)
                )
                if counter_update.rowcount == 0:
                    await session.execute(
                        sa_insert(_LEASE_FENCING).values(
                            tenant_id=tenant_id,
                            resource=resource,
                            counter=1,
                        )
                    )
                    fencing = 1
                else:
                    fencing = int(
                        (
                            await session.execute(
                                sa_select(_LEASE_FENCING.c.counter).where(
                                    _LEASE_FENCING.c.tenant_id == tenant_id,
                                    _LEASE_FENCING.c.resource == resource,
                                )
                            )
                        ).scalar_one()
                    )

                values = {
                    "owner_token": owner_token,
                    "fencing_token": fencing,
                    "acquired_at": now,
                    "expires_at": expires_at,
                }
                claimed = await session.execute(
                    sa_update(_LEASES)
                    .where(
                        _LEASES.c.tenant_id == tenant_id,
                        _LEASES.c.resource == resource,
                        _LEASES.c.expires_at <= now,
                    )
                    .values(**values)
                )
                if claimed.rowcount != 1:
                    # The primary key makes this an atomic insert-or-fail for an
                    # absent lease. An active or concurrently inserted lease
                    # raises IntegrityError and makes this contender lose.
                    await session.execute(
                        sa_insert(_LEASES).values(
                            tenant_id=tenant_id,
                            resource=resource,
                            **values,
                        )
                    )
        except IntegrityError:
            return None
        return LeaseRecord(
            resource=resource,
            owner_token=owner_token,
            fencing_token=fencing,
            acquired_at=now,
            expires_at=expires_at,
        )

    @traced_repository_operation("lease_renew")
    async def renew(
        self,
        lease: LeaseRecord,
        lease_duration: timedelta,
        *,
        context: StorageOperationContext,
    ) -> LeaseRecord:
        self._validate_duration(lease_duration)
        now = datetime.now(UTC)
        expires_at = now + lease_duration
        async with self._session() as session:
            result = await session.execute(
                sa_update(_LEASES)
                .where(
                    _LEASES.c.tenant_id == str(context.tenant_id),
                    _LEASES.c.resource == lease.resource,
                    _LEASES.c.owner_token == lease.owner_token,
                    _LEASES.c.fencing_token == lease.fencing_token,
                    _LEASES.c.expires_at > now,
                )
                .values(expires_at=expires_at)
            )
        if result.rowcount != 1:
            raise HarborStorageLeaseError(
                f"lease {lease.resource!r} is not owned or has expired",
                context=self._backend.error_context("lease_renew", context, lease.resource),
            )
        return lease.model_copy(update={"expires_at": expires_at})

    @traced_repository_operation("lease_release")
    async def release(
        self,
        lease: LeaseRecord,
        *,
        context: StorageOperationContext,
    ) -> bool:
        async with self._session() as session:
            result = await session.execute(
                sa_delete(_LEASES).where(
                    _LEASES.c.tenant_id == str(context.tenant_id),
                    _LEASES.c.resource == lease.resource,
                    _LEASES.c.owner_token == lease.owner_token,
                    _LEASES.c.fencing_token == lease.fencing_token,
                )
            )
        return bool(result.rowcount == 1)

    @staticmethod
    def _validate_duration(lease_duration: timedelta) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")


class SQLStateBackend(HarborStateBackend):
    """Composes operational state, checkpoint, and lease stores over SQLAlchemy."""

    def __init__(
        self,
        *,
        client: SQLAlchemyDBClient,
        instance_name: str,
        create_schema: bool,
        telemetry: StorageTelemetryHook | None = None,
    ) -> None:
        self.client = client
        self._instance_name = instance_name
        self._create_schema = create_schema
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.STATE,
            backend=client.backend,
        )
        self.state = SQLStateStore(self)
        self.checkpoints = SQLCheckpointStore(self)
        self.leases = SQLLeaseStore(self)

    async def connect(self) -> None:
        await self.client.connect()
        if self._create_schema:
            await self.client.create_schema(_METADATA)

    async def close(self) -> None:
        await self.client.close()

    async def health(self) -> RepositoryHealth:
        try:
            await self.client.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, Any] = {}
        except Exception as exc:  # pragma: no cover - integration behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.STATE,
            backend=self.client.backend,
            instance_name=self._instance_name,
            status=status,
            details=details,
        )

    def error_context(
        self,
        operation: str,
        context: StorageOperationContext,
        resource: str,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.STATE,
            backend=self.client.backend,
            instance_name=self._instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id),
            resource_name=resource,
        )
