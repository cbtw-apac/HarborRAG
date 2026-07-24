"""State identifiers and optimistic persistence for ingestion workflows."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
)
from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.ids import TenantId, WorkflowId
from harborrag_core.schemas.state import WorkflowState, WorkflowStatus
from harborrag_core.schemas.storage import StorageOperationContext

from .artifact_objects import digest
from .ingestioncodec import dump_payload
from .schemas import ArtifactActivityInput, ArtifactReference, ArtifactStage, DiscoveryInput


class IngestionStateStorageMixin:
    """Provide stable keys and optimistic state-store updates."""

    async def _active_state(
        self,
        request: ArtifactActivityInput,
    ) -> WorkflowState | None:
        return await self._states.get(
            self._active_id(request),
            context=self._context(request.tenant_id, request.run_id),
        )

    async def _upsert_state(
        self,
        workflow_id: WorkflowId,
        tenant_id: str,
        context: StorageOperationContext,
        *,
        current_step: str,
        payload: dict[str, Any],
    ) -> WorkflowState:
        for _ in range(5):
            current = await self._states.get(workflow_id, context=context)
            if current is None:
                state = WorkflowState(
                    workflow_id=workflow_id,
                    tenant_id=TenantId(tenant_id),
                    status=WorkflowStatus.RUNNING,
                    current_step=current_step,
                    payload=payload,
                )
                try:
                    return await self._states.create(state, context=context)
                except HarborStorageAlreadyExistsError:
                    continue
            try:
                return await self._states.save(
                    current.model_copy(
                        update={"current_step": current_step, "payload": payload}
                    ),
                    expected_version=current.version,
                    context=context,
                )
            except HarborStorageCheckpointConflictError:
                continue
        raise RuntimeError("could not save ingestion state after concurrent updates")

    async def _create_idempotently(
        self,
        state: WorkflowState,
        context: StorageOperationContext,
    ) -> WorkflowState:
        try:
            return await self._states.create(state, context=context)
        except HarborStorageAlreadyExistsError:
            existing = await self._states.get(state.workflow_id, context=context)
            if existing is None:
                raise RuntimeError("ingestion state disappeared after create conflict")
            return existing

    @staticmethod
    def _revision_id(artifact: ArtifactReference, source: SourceRecord) -> str:
        if artifact.artifact_revision_id:
            return artifact.artifact_revision_id
        if artifact.checksum or source.checksum:
            return str(artifact.checksum or source.checksum)
        value = dump_payload(
            "source-revision",
            {
                "id": source.id,
                "locator": source.locator,
                "updated_at": source.updated_at,
                "metadata": source.metadata,
            },
        )
        return sha256(value).hexdigest()

    @staticmethod
    def _context(tenant_id: str, run_id: str) -> StorageOperationContext:
        return StorageOperationContext(
            tenant_id=TenantId(tenant_id),
            workflow_id=WorkflowId(run_id),
            ingestion_job_id=run_id,
        )

    @staticmethod
    def _workflow_id(*parts: str) -> WorkflowId:
        return WorkflowId(f"ing-{digest(chr(0).join(parts))}")

    def _discovery_id(self, request: DiscoveryInput) -> WorkflowId:
        return self._workflow_id("discovery", request.run_id, request.cursor or "start")

    def _stage_id(
        self,
        request: ArtifactActivityInput,
        stage: ArtifactStage,
    ) -> WorkflowId:
        return self._workflow_id(
            "stage",
            request.run_id,
            request.state.artifact.artifact_id,
            stage.value,
        )

    def _active_id(self, request: ArtifactActivityInput) -> WorkflowId:
        return self._workflow_id(
            "active",
            request.tenant_id,
            request.state.artifact.artifact_id,
        )

    @staticmethod
    def _artifact_key(run_id: str, artifact_id: str, kind: str) -> str:
        return f"runs/{digest(run_id)}/artifacts/{digest(artifact_id)}/{kind}.json"

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return str(value).strip() if value is not None and str(value).strip() else None
