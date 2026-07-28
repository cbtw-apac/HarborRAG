from __future__ import annotations

from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.errors import (
    HarborStorageCheckpointConflictError,
)
from harborrag_adapters.repositories.policies.tenancy import ensure_tenant
from harborrag_adapters.repositories.state.base import (
    HarborCheckpointStore,
)
from harborrag_adapters.repositories.telemetry import (
    traced_repository_operation,
)
from harborrag_core.schemas.ids import WorkflowId
from harborrag_core.schemas.state import CheckpointRecord
from harborrag_core.schemas.storage import (
    StorageOperationContext,
)

from .sql_base import SQLStoreBase
from .sql_mapping import _checkpoint_from_row
from .sql_schema import _CHECKPOINTS


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
        error_context.metadata = {
            "expected_version": expected,
            "actual_version": actual,
        }
        return HarborStorageCheckpointConflictError(
            f"checkpoint stream {workflow_id!r} version conflict",
            context=error_context,
        )
