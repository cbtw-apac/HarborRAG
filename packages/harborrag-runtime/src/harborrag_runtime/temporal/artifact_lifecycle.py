"""Validation, finalization, reconciliation, and resolution state transitions."""

from __future__ import annotations

from dataclasses import replace

from harborrag_engine.ingestion.indexing.schemas import (
    GenerationActivationRequest,
    IndexingStatus,
)

from .artifact_objects import digest
from .ingestioncodec import dump_payload, load_indexing_result
from .schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactStage,
    ArtifactStatus,
    ReconciliationInput,
    ReconciliationResult,
    ResolutionDecision,
    ResolutionReceipt,
    RunStatus,
)
from .state_mixin_base import IngestionStateMixinBase


class ArtifactLifecycleMixin(IngestionStateMixinBase):
    """Complete an artifact and reconcile its ingestion run."""

    async def validate(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        reference = request.state.indexing_result_ref
        if reference is None:
            raise ValueError("validation requires indexing_result_ref")
        result = load_indexing_result(
            await self._objects.get(
                reference,
                expected_tenant_id=request.tenant_id,
                expected_key_suffix="/indexing.json",
            )
        )
        valid = (
            result.status is IndexingStatus.SUCCEEDED and result.vector_valid and result.graph_valid
        )
        return ArtifactActivityResult(
            status=ArtifactStatus.RUNNING if valid else ArtifactStatus.QUARANTINED,
            state=replace(request.state, stage=ArtifactStage.FINALIZE),
            error_type=None if valid else "index_validation_failed",
            error_message=None if valid else "; ".join(result.validation_errors),
            retryable=False,
        )

    async def finalize(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        if request.state.chunking_result_ref is None or request.state.indexing_result_ref is None:
            raise ValueError("finalization requires chunking and indexing result references")
        indexing = load_indexing_result(
            await self._objects.get(
                request.state.indexing_result_ref,
                expected_tenant_id=request.tenant_id,
                expected_key_suffix="/indexing.json",
            )
        )
        if (
            indexing.status is not IndexingStatus.SUCCEEDED
            or not indexing.vector_valid
            or not indexing.graph_valid
        ):
            return ArtifactActivityResult(
                status=ArtifactStatus.QUARANTINED,
                state=request.state,
                error_type="index_validation_failed",
                error_message="; ".join(indexing.validation_errors),
            )
        if (
            indexing.artifact_id != request.state.artifact.artifact_id
            or indexing.generation_id != request.state.generation_id
            or indexing.activation.artifact_id != indexing.artifact_id
            or indexing.activation.generation_id != indexing.generation_id
        ):
            raise ValueError("indexing activation identity does not match finalization state")
        if self._activator is None:
            raise RuntimeError("index generation activation service is not configured")
        promotion_started = await self._begin_promotion(request, indexing)
        if promotion_started:
            await self._activator.activate(
                GenerationActivationRequest(
                    plan=indexing.activation,
                    context=self._context(request.tenant_id, request.run_id),
                )
            )
        promoted = promotion_started and await self._finish_promotion(request)
        return ArtifactActivityResult(
            status=ArtifactStatus.SUCCEEDED if promoted else ArtifactStatus.SKIPPED,
            state=request.state,
            error_type=None if promoted else "stale_generation",
            error_message=(
                None if promoted else "a newer generation reserved this artifact before promotion"
            ),
        )

    async def reconcile(
        self,
        request: ReconciliationInput,
    ) -> ReconciliationResult:
        status = (
            RunStatus.CANCELLED
            if request.cancelled
            else RunStatus.FAILED
            if request.progress.failed
            else RunStatus.COMPLETED
        )
        key = f"runs/{digest(request.run_id)}/reconciliation.json"
        reference = await self._objects.put(
            request.tenant_id,
            key,
            dump_payload("reconciliation", {"request": request, "status": status}),
            kind="reconciliation",
        )
        return ReconciliationResult(reference, status)

    async def apply_resolution(
        self,
        request: ArtifactActivityInput,
        decision: ResolutionDecision,
    ) -> ResolutionReceipt:
        accepted = decision.decision.lower() in {
            "approve",
            "continue",
            "retry",
            "skip",
        }
        resume_stage = (
            ArtifactStage.FINALIZE if decision.decision.lower() == "skip" else request.state.stage
        )
        reference = await self._objects.put(
            request.tenant_id,
            self._artifact_key(request.run_id, decision.artifact_id, "resolution"),
            dump_payload("resolution-decision", decision),
            kind="resolution-decision",
        )
        return ResolutionReceipt(
            decision.artifact_id,
            reference,
            accepted,
            resume_stage,
        )

    async def health(self) -> dict[str, object]:
        state_health, object_health = (
            await self._backend.health(),
            await self._objects.store.health(),
        )
        return {
            "ready": state_health.status.value == "healthy"
            and object_health.status.value == "healthy",
            "state": state_health.status.value,
            "objects": object_health.status.value,
        }
