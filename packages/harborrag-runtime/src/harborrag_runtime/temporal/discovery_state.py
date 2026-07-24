"""Durable discovery checkpoints and completed-stage records."""

from __future__ import annotations

from urllib.parse import quote

from pydantic_core import to_jsonable_python

from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.state import WorkflowState, WorkflowStatus

from .ingestioncodec import (
    dump_payload,
    load_activity_result,
    load_discovery_result,
)
from .schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactReference,
    ArtifactStage,
    DiscoveryInput,
    DiscoveryResult,
)


class DiscoveryStateMixin:
    """Persist discovery progress and idempotent stage completion."""

    async def initialize_run(self, request: DiscoveryInput) -> None:
        workflow_id = self._workflow_id("run", request.run_id)
        context = self._context(request.tenant_id, request.run_id)
        state = WorkflowState(
            workflow_id=workflow_id,
            tenant_id=TenantId(request.tenant_id),
            status=WorkflowStatus.RUNNING,
            current_step="discovery",
            payload={
                "run_id": request.run_id,
                "manifest_id": request.manifest_id,
                "connector_name": request.connector_name,
            },
        )
        await self._create_idempotently(state, context)

    async def persist_discovered(
        self,
        request: DiscoveryInput,
        source: SourceRecord,
    ) -> ArtifactReference:
        key = self._artifact_key(request.run_id, source.id, "source")
        source_ref = await self._objects.put(
            request.tenant_id,
            key,
            dump_payload("source-record", source),
            kind="source-record",
        )
        metadata = source.metadata
        source_kind = self._optional_text(metadata.get("source_kind")) or source.source_type
        return ArtifactReference(
            artifact_id=source.id,
            source_ref=source_ref,
            source_kind=source_kind,
            connector_name=request.connector_name,
            artifact_revision_id=self._optional_text(metadata.get("revision_id")),
            checksum=source.checksum,
            parser_hint=self._optional_text(metadata.get("parser_hint")),
            requires_ocr=bool(metadata.get("requires_ocr", False)),
        )

    async def discovery_progress(
        self,
        request: DiscoveryInput,
    ) -> DiscoveryResult | None:
        state = await self._states.get(
            self._discovery_id(request),
            context=self._context(request.tenant_id, request.run_id),
        )
        value = state.payload.get("result") if state else None
        return load_discovery_result(value) if isinstance(value, dict) else None

    async def save_discovery_progress(
        self,
        request: DiscoveryInput,
        artifacts: tuple[ArtifactReference, ...],
        next_cursor: str | None,
        *,
        done: bool,
    ) -> str:
        workflow_id = self._discovery_id(request)
        context = self._context(request.tenant_id, request.run_id)
        checkpoint_ref = (
            f"harbor-state://{quote(request.tenant_id, safe='')}/{workflow_id}"
        )
        result = DiscoveryResult(artifacts, next_cursor, checkpoint_ref, done)
        await self._upsert_state(
            workflow_id,
            request.tenant_id,
            context,
            current_step="discovery-complete" if done else "discovery",
            payload={"result": to_jsonable_python(result)},
        )
        return checkpoint_ref

    async def completed_stage(
        self,
        request: ArtifactActivityInput,
        stage: ArtifactStage,
    ) -> ArtifactActivityResult | None:
        state = await self._states.get(
            self._stage_id(request, stage),
            context=self._context(request.tenant_id, request.run_id),
        )
        value = state.payload.get("result") if state else None
        return load_activity_result(value) if isinstance(value, dict) else None

    async def complete_stage(
        self,
        request: ArtifactActivityInput,
        result: ArtifactActivityResult,
    ) -> ArtifactActivityResult:
        workflow_id = self._stage_id(request, request.state.stage)
        context = self._context(request.tenant_id, request.run_id)
        state = WorkflowState(
            workflow_id=workflow_id,
            tenant_id=TenantId(request.tenant_id),
            status=WorkflowStatus.COMPLETED,
            current_step=request.state.stage.value,
            payload={"result": to_jsonable_python(result)},
        )
        stored = await self._create_idempotently(state, context)
        value = stored.payload.get("result")
        if not isinstance(value, dict):
            raise ValueError("completed ingestion stage has no result payload")
        return load_activity_result(value)
