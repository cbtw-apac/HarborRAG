"""Repository-backed, restart-safe state for Temporal ingestion activities."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
)
from harborrag_adapters.repositories.object_store.base import HarborObjectStore
from harborrag_adapters.repositories.state.base import HarborStateBackend, HarborStateStore
from harborrag_core.domain.document import Document
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.documents import ChunkRecord
from harborrag_core.schemas.ids import TenantId, WorkflowId
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.schemas.state import WorkflowState, WorkflowStatus
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion.chunking.manifest import (
    CanonicalChunkRepository,
    ChunkManifestRepository,
)
from harborrag_engine.ingestion.chunking.schemas import ChunkingResult, ChunkManifest
from harborrag_engine.ingestion.indexing.activation import IndexGenerationActivationService
from harborrag_engine.ingestion.indexing.config import IndexingConfig
from harborrag_engine.ingestion.indexing.schemas import (
    GenerationActivationRequest,
    IndexingRequest,
    IndexingResult,
    IndexingStatus,
)
from pydantic_core import to_jsonable_python

from harborrag_runtime.temporal.ingestioncodec import (
    dump_parsed_document,
    dump_payload,
    dump_raw_document,
    load_activity_result,
    load_chunk_manifest,
    load_chunking_result,
    load_discovery_result,
    load_document,
    load_indexing_result,
    load_payload,
    load_raw_document,
    load_source_record,
)
from harborrag_runtime.temporal.models import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactReference,
    ArtifactStage,
    ArtifactStatus,
    DiscoveryInput,
    DiscoveryResult,
    ReconciliationInput,
    ReconciliationResult,
    ResolutionDecision,
    ResolutionReceipt,
    RunStatus,
)

_BUCKET = "ingestion"


class IngestionObjectRepository:
    """Store typed ingestion payloads behind compact, tenant-bearing references."""

    def __init__(self, store: HarborObjectStore) -> None:
        self.store = store

    async def put(
        self,
        tenant_id: str,
        key: str,
        payload: bytes,
        *,
        kind: str,
    ) -> str:
        context = self.context(tenant_id)
        await self.store.put(
            PutObjectRequest(
                bucket=_BUCKET,
                key=key,
                body=payload,
                content_type="application/json",
                metadata={"kind": kind},
            ),
            context=context,
        )
        return self.reference(tenant_id, key)

    async def get(self, reference: str) -> bytes:
        tenant_id, bucket, key = self.parts(reference)
        return await self.store.get_bytes(
            bucket,
            key,
            byte_range=None,
            context=self.context(tenant_id),
        )

    async def exists(self, tenant_id: str, key: str) -> bool:
        return await self.store.exists(
            _BUCKET,
            key,
            context=self.context(tenant_id),
        )

    @staticmethod
    def context(tenant_id: str) -> StorageOperationContext:
        return StorageOperationContext(tenant_id=TenantId(tenant_id))

    @staticmethod
    def reference(tenant_id: str, key: str) -> str:
        return f"harbor-object://{quote(tenant_id, safe='')}/{_BUCKET}/{key}"

    @staticmethod
    def parts(reference: str) -> tuple[str, str, str]:
        parsed = urlsplit(reference)
        path = parsed.path.lstrip("/").split("/", 1)
        if parsed.scheme != "harbor-object" or not parsed.netloc or len(path) != 2:
            raise ValueError(f"invalid ingestion object reference: {reference!r}")
        return unquote(parsed.netloc), path[0], path[1]


class ObjectChunkRepository(CanonicalChunkRepository):
    """Persist immutable canonical chunk bodies in the configured object store."""

    def __init__(self, objects: IngestionObjectRepository) -> None:
        self._objects = objects

    async def put(self, records: tuple[ChunkRecord, ...]) -> None:
        for record in records:
            key = self._key(str(record.chunk_revision_id))
            await self._objects.put(
                str(record.tenant_id),
                key,
                dump_payload("canonical-chunk", record),
                kind="canonical-chunk",
            )

    async def get_many(
        self,
        tenant_id: str,
        chunk_revision_ids: tuple[str, ...],
    ) -> tuple[ChunkRecord, ...]:
        records: list[ChunkRecord] = []
        for revision_id in chunk_revision_ids:
            reference = self._objects.reference(tenant_id, self._key(revision_id))
            value = load_payload(await self._objects.get(reference), "canonical-chunk")
            records.append(ChunkRecord.model_validate(value))
        return tuple(records)

    @staticmethod
    def _key(revision_id: str) -> str:
        return f"chunks/{_digest(revision_id)}.json"


class ObjectManifestRepository(ChunkManifestRepository):
    """Persist chunk manifests independently from their canonical bodies."""

    def __init__(self, objects: IngestionObjectRepository) -> None:
        self._objects = objects

    async def put(self, manifest: ChunkManifest) -> None:
        await self._objects.put(
            manifest.tenant_id,
            self._key(
                manifest.artifact_id,
                manifest.artifact_revision_id,
                manifest.configuration_hash,
            ),
            dump_payload("chunk-manifest", manifest),
            kind="chunk-manifest",
        )

    async def get(
        self,
        tenant_id: str,
        artifact_id: str,
        artifact_revision_id: str,
        configuration_hash: str,
    ) -> ChunkManifest | None:
        key = self._key(artifact_id, artifact_revision_id, configuration_hash)
        if not await self._objects.exists(tenant_id, key):
            return None
        reference = self._objects.reference(tenant_id, key)
        value = load_payload(await self._objects.get(reference), "chunk-manifest")
        return load_chunk_manifest(value)

    @staticmethod
    def _key(artifact_id: str, revision_id: str, configuration_hash: str) -> str:
        identity = f"{artifact_id}\0{revision_id}\0{configuration_hash}"
        return f"manifests/{_digest(identity)}.json"


class RepositoryRuntimeIngestionState:
    """Coordinate idempotent stages using state and object repositories."""

    def __init__(
        self,
        state_backend: HarborStateBackend,
        objects: IngestionObjectRepository,
        indexing_config: IndexingConfig,
        activator: IndexGenerationActivationService | None = None,
        *,
        chunking_configuration_version: str = "1",
    ) -> None:
        self._backend = state_backend
        self._states: HarborStateStore = state_backend.state
        self._objects = objects
        self._indexing_config = indexing_config
        self._activator = activator
        if not chunking_configuration_version.strip():
            raise ValueError("chunking_configuration_version must be non-empty")
        self._chunking_configuration_version = chunking_configuration_version

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
        checkpoint_ref = f"harbor-state://{quote(request.tenant_id, safe='')}/{workflow_id}"
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

    async def preflight(self, request: ArtifactActivityInput) -> ArtifactActivityResult:
        source = await self.load_source(request.state.artifact.source_ref)
        revision_id = self._revision_id(request.state.artifact, source)
        active = await self._active_state(request)
        if active and (
            active.payload.get("active_revision_id") == revision_id
            and active.payload.get("active_indexing_fingerprint")
            == self._indexing_config.configuration_fingerprint
            and active.payload.get("active_chunking_configuration_version")
            == self._chunking_configuration_version
        ):
            return ArtifactActivityResult(
                status=ArtifactStatus.UNCHANGED,
                state=replace(request.state, artifact_revision_id=revision_id),
            )
        await self._reserve_generation(request, revision_id)
        return ArtifactActivityResult(
            status=ArtifactStatus.RUNNING,
            state=replace(
                request.state,
                stage=ArtifactStage.FETCH,
                artifact_revision_id=revision_id,
            ),
        )

    async def load_source(self, source_ref: str) -> SourceRecord:
        return load_source_record(await self._objects.get(source_ref))

    async def persist_snapshot(
        self,
        request: ArtifactActivityInput,
        document: RawDocument,
    ) -> str:
        return await self._objects.put(
            request.tenant_id,
            self._artifact_key(request.run_id, request.state.artifact.artifact_id, "snapshot"),
            dump_raw_document(document),
            kind="raw-document",
        )

    async def load_snapshot(self, snapshot_ref: str) -> RawDocument:
        return load_raw_document(await self._objects.get(snapshot_ref))

    async def persist_parsed_document(
        self,
        request: ArtifactActivityInput,
        parsed: ParsedDocument,
        document: Document,
    ) -> str:
        return await self._objects.put(
            request.tenant_id,
            self._artifact_key(request.run_id, request.state.artifact.artifact_id, "parsed"),
            dump_parsed_document(parsed, document),
            kind="parsed-document",
        )

    async def load_parsed_document(self, parsed_document_ref: str) -> Document:
        return load_document(await self._objects.get(parsed_document_ref))

    async def persist_chunking_result(
        self,
        request: ArtifactActivityInput,
        result: ChunkingResult,
    ) -> str:
        return await self._objects.put(
            request.tenant_id,
            self._artifact_key(request.run_id, request.state.artifact.artifact_id, "chunking"),
            dump_payload("chunking-result", result),
            kind="chunking-result",
        )

    async def load_chunking_result(self, chunking_result_ref: str) -> ChunkingResult:
        return load_chunking_result(await self._objects.get(chunking_result_ref))

    async def indexing_request(
        self,
        request: ArtifactActivityInput,
        chunking: ChunkingResult,
    ) -> IndexingRequest:
        active = await self._active_state(request)
        active_chunking = None
        active_ref = active.payload.get("active_chunking_ref") if active else None
        if isinstance(active_ref, str):
            active_chunking = await self.load_chunking_result(active_ref)
        return IndexingRequest(
            chunking=chunking,
            generation_id=request.state.generation_id,
            config=self._indexing_config,
            context=self._context(request.tenant_id, request.run_id),
            active_manifest=(active_chunking.manifest if active_chunking else None),
            active_embedding_configuration_fingerprint=(
                self._optional_text(active.payload.get("active_embedding_fingerprint"))
                if active
                else None
            ),
            active_generation_id=(
                self._optional_text(active.payload.get("active_generation_id")) if active else None
            ),
        )

    async def persist_indexing_result(
        self,
        request: ArtifactActivityInput,
        result: IndexingResult,
    ) -> str:
        return await self._objects.put(
            request.tenant_id,
            self._artifact_key(request.run_id, request.state.artifact.artifact_id, "indexing"),
            dump_payload("indexing-result", result),
            kind="indexing-result",
        )

    async def validate(self, request: ArtifactActivityInput) -> ArtifactActivityResult:
        reference = request.state.indexing_result_ref
        if reference is None:
            raise ValueError("validation requires indexing_result_ref")
        result = load_indexing_result(await self._objects.get(reference))
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

    async def finalize(self, request: ArtifactActivityInput) -> ArtifactActivityResult:
        if request.state.chunking_result_ref is None or request.state.indexing_result_ref is None:
            raise ValueError("finalization requires chunking and indexing result references")
        indexing = load_indexing_result(await self._objects.get(request.state.indexing_result_ref))
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

    async def reconcile(self, request: ReconciliationInput) -> ReconciliationResult:
        status = (
            RunStatus.CANCELLED
            if request.cancelled
            else RunStatus.FAILED
            if request.progress.failed
            else RunStatus.COMPLETED
        )
        key = f"runs/{_digest(request.run_id)}/reconciliation.json"
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
        accepted = decision.decision.lower() in {"approve", "continue", "retry", "skip"}
        resume_stage = (
            ArtifactStage.FINALIZE if decision.decision.lower() == "skip" else request.state.stage
        )
        reference = await self._objects.put(
            request.tenant_id,
            self._artifact_key(request.run_id, decision.artifact_id, "resolution"),
            dump_payload("resolution-decision", decision),
            kind="resolution-decision",
        )
        return ResolutionReceipt(decision.artifact_id, reference, accepted, resume_stage)

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
            active_generation = self._optional_text(
                current.payload.get("active_generation_id")
            )
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
                current.payload.get("promotion_generation_id")
                != request.state.generation_id
                or current.payload.get("pending_generation_id")
                != request.state.generation_id
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
                    "active_chunking_configuration_version": (
                        self._chunking_configuration_version
                    ),
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

    async def _active_state(self, request: ArtifactActivityInput) -> WorkflowState | None:
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
                    current.model_copy(update={"current_step": current_step, "payload": payload}),
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
        return WorkflowId(f"ing-{_digest(chr(0).join(parts))}")

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
        return f"runs/{_digest(run_id)}/artifacts/{_digest(artifact_id)}/{kind}.json"

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return str(value).strip() if value is not None and str(value).strip() else None


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
