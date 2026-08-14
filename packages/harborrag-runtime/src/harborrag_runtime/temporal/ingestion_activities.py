from __future__ import annotations

from temporalio import activity

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.observability import IngestionTelemetry

from .activity_observability import ActivityObservability
from .conversion import (
    to_capture_stage,
    to_prepared_document,
    to_prepared_stage,
    to_raw_capture_result,
)
from .heartbeats import heartbeat_while, last_heartbeat_detail
from .plan_resolver import PlanDocumentResolver
from .process_isolation import SubprocessCrashError, run_in_isolated_subprocess
from .retry_activities import RetryActivitiesMixin
from .schemas import (
    DocumentFailureInput,
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
)
from .source_activities import SourceActivitiesMixin


class IngestionActivities(RetryActivitiesMixin, SourceActivitiesMixin):
    """Thin Temporal boundaries around durable ingestion application services."""

    def __init__(
        self,
        runtime: IngestionRuntime,
        *,
        telemetry: IngestionTelemetry | None = None,
    ) -> None:
        self._runtime = runtime
        self._observability = ActivityObservability(telemetry or IngestionTelemetry())
        self._documents = PlanDocumentResolver(runtime.source_plans)

    @activity.defn(name="harborrag.fetch_and_capture_raw")
    async def fetch_and_capture_raw(
        self,
        request: DocumentIngestionInput,
    ) -> RawCaptureResult:
        with self._observability.boundary("FetchAndCaptureRaw"):
            planned = await self._documents.get(request)
            result = await heartbeat_while(
                self._runtime.stages.preparation.fetch_and_capture(
                    planned.request,
                    self._runtime.connector(
                        request.connector_name,
                        configuration_fingerprint=(planned.request.configuration_fingerprint),
                    ),
                ),
                detail="fetch-and-capture",
            )
            self._observability.record_capture(
                result,
                planned.request.source_identity.connector_type.value,
            )
            return to_raw_capture_result(request, result)

    @activity.defn(name="harborrag.parse_and_normalize")
    async def parse_and_normalize(
        self,
        request: RawCaptureResult,
    ) -> PreparedDocument:
        with self._observability.boundary("ParseAndNormalize"):
            planned = await self._documents.get(request.document)
            capture_stage = to_capture_stage(request)
            prior = last_heartbeat_detail()
            heartbeat_detail = {
                "stage": "parse-and-normalize",
                "task_id": request.document.task_id,
                "document_id": request.document_id,
                "document_index": request.document.document_index,
                "mode": "subprocess",
                "resumed": prior is not None,
                "prior": prior,
            }
            try:
                prepared_stage = await run_in_isolated_subprocess(
                    _parse_and_normalize_sync,
                    self._runtime.stages.preparation,
                    planned.request,
                    capture_stage,
                    heartbeat_detail=heartbeat_detail,
                )
                self._observability.record_subprocess_outcome(
                    "ParseAndNormalize",
                    "success",
                )
            except SubprocessCrashError as error:
                if "serialization failed" not in str(error):
                    self._observability.record_subprocess_outcome(
                        "ParseAndNormalize",
                        "crash",
                    )
                    raise

                self._observability.record_subprocess_outcome(
                    "ParseAndNormalize",
                    "serialization_fail",
                )
                self._observability.record_subprocess_fallback(
                    "ParseAndNormalize",
                    "serialization_fail",
                )
                prepared_stage = await heartbeat_while(
                    self._runtime.stages.preparation.parse_and_normalize(
                        planned.request,
                        capture_stage,
                    ),
                    detail={
                        **heartbeat_detail,
                        "mode": "in-process-fallback",
                        "fallback_reason": "spawn-unpicklable-args",
                    },
                )
            self._observability.record_prepared(prepared_stage)
            return to_prepared_document(request.document, prepared_stage)

    @activity.defn(name="harborrag.sync_content_units")
    async def sync_content_units(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("SyncContentUnits"):
            planned = await self._documents.get(request.document)
            await heartbeat_while(
                self._runtime.stages.preparation.sync_content_units(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="sync-content-units",
            )
            return request

    @activity.defn(name="harborrag.persist_canonical")
    async def persist_canonical(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("PersistCanonical"):
            planned = await self._documents.get(request.document)
            await heartbeat_while(
                self._runtime.stages.preparation.persist_canonical(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="persist-canonical",
            )
            return request

    @activity.defn(name="harborrag.chunk_and_validate")
    async def chunk_and_validate(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("ChunkAndValidate"):
            planned = await self._documents.get(request.document)
            statistics = await heartbeat_while(
                self._runtime.stages.preparation.chunk_and_validate(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="chunk-and-validate",
            )
            self._observability.record_chunking(statistics)
            return request

    @activity.defn(name="harborrag.encode_chunks")
    async def encode_chunks(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("EncodeChunks"):
            planned = await self._documents.get(request.document)
            await heartbeat_while(
                self._runtime.stages.preparation.encode_chunks(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="encode-chunks",
            )
            return request

    @activity.defn(name="harborrag.build_relations")
    async def build_relations(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("BuildRelations"):
            planned = await self._documents.get(request.document)
            await heartbeat_while(
                self._runtime.stages.projections.build_relations(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="build-relations",
            )
            return request

    @activity.defn(name="harborrag.build_projections")
    async def build_projections(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("BuildProjections"):
            planned = await self._documents.get(request.document)
            await heartbeat_while(
                self._runtime.stages.projections.build_projections(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="build-projections",
            )
            return request

    @activity.defn(name="harborrag.write_vector_projection")
    async def write_vector_projection(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("WriteVectorProjection"):
            planned = await self._documents.get(request.document)
            await heartbeat_while(
                self._runtime.stages.projections.write_vector_projection(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="write-vector-projection",
            )
            return request

    @activity.defn(name="harborrag.write_graph_projection")
    async def write_graph_projection(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("WriteGraphProjection"):
            planned = await self._documents.get(request.document)
            await heartbeat_while(
                self._runtime.stages.projections.write_graph_projection(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="write-graph-projection",
            )
            return request

    @activity.defn(name="harborrag.verify_projections")
    async def verify_projections(
        self,
        request: PreparedDocument,
    ) -> PreparedDocument:
        with self._observability.boundary("VerifyProjections"):
            planned = await self._documents.get(request.document)
            await heartbeat_while(
                self._runtime.stages.projections.verify_projections(
                    planned.request,
                    to_prepared_stage(request),
                ),
                detail="verify-projections",
            )
            return request

    @activity.defn(name="harborrag.publish_version")
    async def publish_version(
        self,
        request: PreparedDocument,
    ) -> DocumentIngestionOutcome:
        with self._observability.boundary("PublishVersion"):
            planned = await self._documents.get(request.document)
            outcome = await self._runtime.stages.projections.publish_version(
                to_prepared_stage(request)
            )
            self._observability.record_publication(
                outcome,
                planned.request.source_identity.connector_type.value,
            )
            return await self._runtime.sources.record_published_document(
                request.document.task_id,
                planned,
                outcome,
            )

    @activity.defn(name="harborrag.record_document_failure")
    async def record_document_failure(
        self,
        request: DocumentFailureInput,
    ) -> None:
        with self._observability.boundary("RecordDocumentFailure"):
            planned = await self._documents.get(request.document)
            if request.prepared is not None:
                await self._runtime.stages.preparation.record_failure(
                    to_prepared_stage(request.prepared),
                    stage=request.failed_stage,
                    error=RuntimeError(request.error_type),
                )
            await self._runtime.sources.record_failed_document(
                request.document.task_id,
                planned,
                error_type=request.error_type,
                failed_stage=request.failed_stage,
            )
            self._observability.record_document_failure(
                planned.request.source_identity.connector_type.value,
            )


def _parse_and_normalize_sync(preparation: object, request: object, capture: object) -> object:
    """Module-level target for subprocess isolation; picklable by the spawn context."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(preparation.parse_and_normalize(request, capture))
    finally:
        loop.close()
