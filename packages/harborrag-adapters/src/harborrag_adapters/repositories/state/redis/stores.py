from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
    HarborStorageLeaseError,
)
from harborrag_adapters.repositories.policies import ensure_tenant
from harborrag_adapters.repositories.state.base import (
    HarborCheckpointStore,
    HarborLeaseStore,
    HarborStateStore,
)
from harborrag_adapters.repositories.state.redis.scripts import (
    ACQUIRE_LEASE,
    RELEASE_LEASE,
    RENEW_LEASE,
)
from harborrag_adapters.repositories.telemetry import traced_repository_operation
from harborrag_core.schemas.ids import WorkflowId
from harborrag_core.schemas.state import CheckpointRecord, LeaseRecord, WorkflowState
from harborrag_core.schemas.storage import StorageOperationContext

WatchError: Any
try:
    from redis.exceptions import WatchError as _WatchError
except ImportError:  # pragma: no cover - optional dependency
    WatchError = RuntimeError
else:
    WatchError = _WatchError

if TYPE_CHECKING:
    from harborrag_adapters.repositories.state.redis.repository import RedisStateBackend


class RedisStoreBase:
    """Holds the shared Redis backend used by cohesive state products."""

    def __init__(self, backend: RedisStateBackend) -> None:
        self._backend = backend
        self._telemetry = backend._telemetry


class RedisStateStore(RedisStoreBase, HarborStateStore):
    """Persists versioned workflow state using Redis transactions."""

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
        key = self._backend.key(context, "workflow", str(state.workflow_id))
        created = await self._backend.client.set(
            key,
            state.model_dump_json(),
            nx=True,
            exat=int(state.expires_at.timestamp()) if state.expires_at else None,
        )
        if not created:
            raise HarborStorageAlreadyExistsError(
                f"workflow state {state.workflow_id!r} already exists",
                context=self._backend.error_context(
                    "state_create", context, str(state.workflow_id)
                ),
            )
        return state

    @traced_repository_operation("state_get")
    async def get(
        self,
        workflow_id: WorkflowId,
        *,
        context: StorageOperationContext,
    ) -> WorkflowState | None:
        raw = await self._backend.client.get(
            self._backend.key(context, "workflow", str(workflow_id))
        )
        return WorkflowState.model_validate_json(raw) if raw else None

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
        key = self._backend.key(context, "workflow", str(state.workflow_id))
        async with self._backend.client.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                existing = WorkflowState.model_validate_json(raw) if raw else None
                if existing is None or existing.version != expected_version:
                    raise self._conflict(context, state.workflow_id, expected_version, existing)
                saved = state.model_copy(
                    update={
                        "version": expected_version + 1,
                        "updated_at": datetime.now(UTC),
                    }
                )
                pipe.multi()
                pipe.set(
                    key,
                    saved.model_dump_json(),
                    exat=(int(saved.expires_at.timestamp()) if saved.expires_at else None),
                )
                await pipe.execute()
                return saved
            except WatchError as exc:
                raise self._conflict(context, state.workflow_id, expected_version, None) from exc

    def _conflict(
        self,
        context: StorageOperationContext,
        workflow_id: WorkflowId,
        expected: int,
        existing: WorkflowState | None,
    ) -> HarborStorageCheckpointConflictError:
        error_context = self._backend.error_context("state_save", context, str(workflow_id))
        error_context.metadata = {
            "expected_version": expected,
            "actual_version": existing.version if existing else None,
        }
        return HarborStorageCheckpointConflictError(
            f"workflow {workflow_id!r} version conflict",
            context=error_context,
        )


class RedisCheckpointStore(RedisStoreBase, HarborCheckpointStore):
    """Persists append-only checkpoints with optimistic concurrency."""

    @traced_repository_operation("checkpoint_load_latest")
    async def load_latest(
        self,
        workflow_id: WorkflowId,
        *,
        context: StorageOperationContext,
    ) -> CheckpointRecord | None:
        raw = await self._backend.client.get(
            self._backend.key(context, "checkpoint_latest", str(workflow_id))
        )
        return CheckpointRecord.model_validate_json(raw) if raw else None

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
        latest_key = self._backend.key(context, "checkpoint_latest", str(checkpoint.workflow_id))
        history_key = self._backend.key(context, "checkpoint_history", str(checkpoint.workflow_id))
        async with self._backend.client.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(latest_key)
                raw = await pipe.get(latest_key)
                existing = CheckpointRecord.model_validate_json(raw) if raw else None
                actual = existing.state_version if existing else None
                required = 1 if actual is None else actual + 1
                if actual != expected_version or checkpoint.state_version != required:
                    raise self._conflict(
                        context,
                        checkpoint.workflow_id,
                        expected_version,
                        actual,
                    )
                payload = checkpoint.model_dump_json()
                pipe.multi()
                pipe.set(latest_key, payload)
                pipe.rpush(history_key, payload)
                await pipe.execute()
                return checkpoint
            except WatchError as exc:
                raise self._conflict(
                    context, checkpoint.workflow_id, expected_version, None
                ) from exc

    def _conflict(
        self,
        context: StorageOperationContext,
        workflow_id: WorkflowId,
        expected: int | None,
        actual: int | None,
    ) -> HarborStorageCheckpointConflictError:
        error_context = self._backend.error_context("checkpoint_save", context, str(workflow_id))
        error_context.metadata = {
            "expected_version": expected,
            "actual_version": actual,
        }
        return HarborStorageCheckpointConflictError(
            f"checkpoint stream {workflow_id!r} version conflict",
            context=error_context,
        )


class RedisLeaseStore(RedisStoreBase, HarborLeaseStore):
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
        lease_key = self._backend.key(context, "lease", resource)
        fencing_key = self._backend.key(context, "fencing", resource)
        duration_ms = int(lease_duration.total_seconds() * 1000)
        fencing = int(
            await self._backend.client.eval(
                ACQUIRE_LEASE,
                2,
                lease_key,
                fencing_key,
                owner_token,
                duration_ms,
            )
        )
        if not fencing:
            return None
        now = datetime.now(UTC)
        return LeaseRecord(
            resource=resource,
            owner_token=owner_token,
            fencing_token=fencing,
            acquired_at=now,
            expires_at=now + lease_duration,
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
        lease_key = self._backend.key(context, "lease", lease.resource)
        duration_ms = int(lease_duration.total_seconds() * 1000)
        renewed = await self._backend.client.eval(
            RENEW_LEASE,
            1,
            lease_key,
            lease.owner_token,
            duration_ms,
        )
        if not renewed:
            raise HarborStorageLeaseError(
                f"lease {lease.resource!r} is not owned or has expired",
                context=self._backend.error_context("lease_renew", context, lease.resource),
            )
        return lease.model_copy(update={"expires_at": datetime.now(UTC) + lease_duration})

    @traced_repository_operation("lease_release")
    async def release(
        self,
        lease: LeaseRecord,
        *,
        context: StorageOperationContext,
    ) -> bool:
        lease_key = self._backend.key(context, "lease", lease.resource)
        released = await self._backend.client.eval(
            RELEASE_LEASE,
            1,
            lease_key,
            lease.owner_token,
        )
        return bool(released)

    @staticmethod
    def _validate_duration(lease_duration: timedelta) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
