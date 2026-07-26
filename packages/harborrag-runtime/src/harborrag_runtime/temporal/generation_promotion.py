"""Optimistic generation reservation and promotion for ingestion state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.state import WorkflowState, WorkflowStatus

from .state_mixin_base import IngestionStateMixinBase

if TYPE_CHECKING:
    from harborrag_engine.ingestion.indexing.schemas import IndexingResult

    from .schemas import ArtifactActivityInput


class GenerationPromotionMixin(IngestionStateMixinBase):
    """Reserve and atomically promote one artifact generation."""

    async def _reserve_generation(
        self,
        request: ArtifactActivityInput,
        revision_id: str,
    ) -> None:
        workflow_id = self._active_id(request)
        context = self._context(request.tenant_id, request.run_id)
        for _ in range(5):
            current = await self._states.get(workflow_id, context=context)
            payload = dict(current.payload) if current else {}
            promotion_generation = self._optional_text(payload.get("promotion_generation_id"))
            if promotion_generation not in {None, request.state.generation_id}:
                raise RuntimeError(
                    "another generation is being promoted for this artifact; retry later"
                )
            payload.update(
                {
                    "artifact_id": request.state.artifact.artifact_id,
                    "pending_generation_id": request.state.generation_id,
                    "pending_revision_id": revision_id,
                }
            )
            if current is None:
                created = WorkflowState(
                    workflow_id=workflow_id,
                    tenant_id=TenantId(request.tenant_id),
                    status=WorkflowStatus.RUNNING,
                    current_step="reserved",
                    payload=payload,
                )
                try:
                    await self._states.create(created, context=context)
                    return
                except HarborStorageAlreadyExistsError:
                    continue
            else:
                try:
                    await self._states.save(
                        current.model_copy(
                            update={
                                "status": WorkflowStatus.RUNNING,
                                "current_step": "reserved",
                                "payload": payload,
                            }
                        ),
                        expected_version=current.version,
                        context=context,
                    )
                    return
                except HarborStorageCheckpointConflictError:
                    continue
        raise RuntimeError("could not reserve artifact generation after concurrent updates")

    async def _begin_promotion(
        self,
        request: ArtifactActivityInput,
        indexing: IndexingResult,
    ) -> bool:
        workflow_id = self._active_id(request)
        context = self._context(request.tenant_id, request.run_id)
        for _ in range(5):
            current = await self._states.get(workflow_id, context=context)
            if current is None:
                raise ValueError("artifact generation was not reserved during preflight")
            if current.payload.get("pending_generation_id") != request.state.generation_id:
                return False
            active_generation = self._optional_text(current.payload.get("active_generation_id"))
            if active_generation != indexing.activation.previous_generation_id:
                return False
            promotion_generation = self._optional_text(
                current.payload.get("promotion_generation_id")
            )
            if promotion_generation is not None:
                return promotion_generation == request.state.generation_id
            payload = dict(current.payload)
            payload.update(
                {
                    "promotion_generation_id": request.state.generation_id,
                    "promotion_revision_id": request.state.artifact_revision_id,
                }
            )
            try:
                await self._states.save(
                    current.model_copy(
                        update={
                            "status": WorkflowStatus.RUNNING,
                            "current_step": "promoting",
                            "payload": payload,
                        }
                    ),
                    expected_version=current.version,
                    context=context,
                )
                return True
            except HarborStorageCheckpointConflictError:
                continue
        raise RuntimeError("could not begin artifact generation promotion")

    async def _finish_promotion(self, request: ArtifactActivityInput) -> bool:
        workflow_id = self._active_id(request)
        context = self._context(request.tenant_id, request.run_id)
        for _ in range(5):
            current = await self._states.get(workflow_id, context=context)
            if current is None:
                raise ValueError("artifact generation promotion state disappeared")
            if (
                current.payload.get("promotion_generation_id") != request.state.generation_id
                or current.payload.get("pending_generation_id") != request.state.generation_id
            ):
                return False
            payload = dict(current.payload)
            payload.update(
                {
                    "active_revision_id": request.state.artifact_revision_id,
                    "active_generation_id": request.state.generation_id,
                    "active_chunking_ref": request.state.chunking_result_ref,
                    "active_embedding_fingerprint": (
                        self._indexing_config.embedding_configuration_fingerprint
                    ),
                    "active_indexing_fingerprint": (
                        self._indexing_config.configuration_fingerprint
                    ),
                    "active_chunking_configuration_version": (self._chunking_configuration_version),
                }
            )
            for key in (
                "pending_generation_id",
                "pending_revision_id",
                "promotion_generation_id",
                "promotion_revision_id",
            ):
                payload.pop(key, None)
            try:
                await self._states.save(
                    current.model_copy(
                        update={
                            "status": WorkflowStatus.COMPLETED,
                            "current_step": "active",
                            "payload": payload,
                        }
                    ),
                    expected_version=current.version,
                    context=context,
                )
                return True
            except HarborStorageCheckpointConflictError:
                continue
        raise RuntimeError("could not finish artifact generation promotion")
