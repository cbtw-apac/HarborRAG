from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
)
from harborrag_adapters.repositories.policies.tenancy import ensure_tenant
from harborrag_adapters.repositories.state.base import (
    HarborStateStore,
)
from harborrag_adapters.repositories.telemetry import (
    traced_repository_operation,
)
from harborrag_core.schemas.ids import WorkflowId
from harborrag_core.schemas.state import WorkflowState
from harborrag_core.schemas.storage import (
    StorageOperationContext,
)

from .sql_base import SQLStoreBase
from .sql_mapping import _state_from_row
from .sql_schema import _WORKFLOW_STATE


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
