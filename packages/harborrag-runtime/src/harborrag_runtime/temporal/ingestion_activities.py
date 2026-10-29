from __future__ import annotations

from dataclasses import replace

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from harborrag_core.contracts.errors import HarborConflictError, HarborValidationError
from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.execution.gateway import prepare_configured_source_submission
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.connections import require_fingerprint
from harborrag_runtime.ingestion.observability import IngestionTelemetry
from harborrag_runtime.ingestion_contracts import SourceSubmission

from .activity_observability import ActivityObservability
from .conversion import (
    to_capture_stage,
    to_prepared_document,
    to_prepared_stage,
    to_raw_capture_result,
)
from .gateway import to_temporal_source
from .heartbeats import heartbeat_while, last_heartbeat_detail
from .plan_resolver import PlanDocumentResolver
from .retry_activities import RetryActivitiesMixin
from .schemas import (
    DocumentFailureInput,
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
    ScheduledRunMetricInput,
    ScheduledSourcePreparation,
    ScheduledSourcePreparationInput,
)
from .source_activities import SourceActivitiesMixin


class IngestionActivities(RetryActivitiesMixin, SourceActivitiesMixin):
    """Thin Temporal boundaries around durable ingestion application services."""

    def __init__(
        self,
        runtime: IngestionRuntime,
        *,
        temporal_client: Client | None = None,
        telemetry: IngestionTelemetry | None = None,
        settings: RuntimeSettings | None = None,
        temporal_config: TemporalRuntimeConfig | None = None,
    ) -> None:
        self._runtime = runtime
        self._settings = settings
        # The config this worker polls with, so children go to queues it serves.
        self._temporal_config = temporal_config
        self._temporal_client = temporal_client
        self._observability = ActivityObservability(telemetry or IngestionTelemetry())
        self._documents = PlanDocumentResolver(runtime.source_plans)

    @activity.defn(name="harborrag.prepare_scheduled_source")
    async def prepare_scheduled_source(
        self,
        request: ScheduledSourcePreparationInput,
    ) -> ScheduledSourcePreparation:
        """Prepare a scheduled firing from this worker's current configuration."""

        settings = self._settings
        temporal_config = self._temporal_config
        if settings is None or temporal_config is None:
            raise ApplicationError(
                "worker was started without runtime settings", non_retryable=True
            )
        resolver = self._runtime.connection_resolver
        if resolver is None:
            raise ApplicationError("worker has no connection store", non_retryable=True)
        try:
            connection = await resolver.resolve(
                tenant_id=request.tenant_id, source_key=request.connection_id
            )
            prepared = prepare_configured_source_submission(
                settings,
                SourceSubmission(
                    task_id=request.task_id,
                    tenant_id=request.tenant_id,
                    connector_name=request.connection_id,
                    connection_id=request.connection_id,
                    source_scope_id=request.source_scope_id,
                    force_reprocess=request.force_reprocess,
                ),
                connection=connection,
            )
        except (ValueError, HarborValidationError, HarborConflictError) as error:
            # A removed, deleting or invalid connection cannot heal on retry.
            raise ApplicationError(
                str(error), type=type(error).__name__, non_retryable=True
            ) from error
        if await self._runtime.control.source_scans.has_running_scan(prepared.source_scope_id):
            return ScheduledSourcePreparation(source=None, source_scope_id=prepared.source_scope_id)
        source = replace(
            to_temporal_source(prepared),
            workflow_options=temporal_config.workflow_options(),
        )
        return ScheduledSourcePreparation(source=source, source_scope_id=prepared.source_scope_id)

    @activity.defn(name="harborrag.record_scheduled_run_metrics")
    async def record_scheduled_run_metrics(self, request: ScheduledRunMetricInput) -> None:
        self._runtime.telemetry.record_scheduled_run(
            request.schedule_id,
            request.outcome,
            schedule_to_start_seconds=request.schedule_to_start_seconds,
        )

    @activity.defn(name="harborrag.fetch_and_capture_raw")
    async def fetch_and_capture_raw(
        self,
        request: DocumentIngestionInput,
    ) -> RawCaptureResult:
        with self._observability.boundary("FetchAndCaptureRaw"):
            planned = await self._documents.get(request)
            connector = await self._runtime.connector(
                tenant_id=request.tenant_id,
                task_id=request.task_id,
                configuration_fingerprint=require_fingerprint(
                    planned.request.configuration_fingerprint
                ),
            )
            result = await heartbeat_while(
                self._runtime.stages.preparation.fetch_and_capture(
                    planned.request,
                    connector,
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
            prior_attempt_count = 0
            if prior is not None:
                prior_attempt_count = 1
                if isinstance(prior, dict):
                    candidate = prior.get("prior_attempt_count")
                    if isinstance(candidate, int) and candidate >= 0:
                        prior_attempt_count = candidate + 1
            heartbeat_detail = {
                "stage": "parse-and-normalize",
                "task_id": request.document.task_id,
                "document_id": request.document_id,
                "document_index": request.document.document_index,
                "mode": "in-process",
                "resumed": prior is not None,
                "prior_attempt_count": prior_attempt_count,
            }
            # Parsing runs in-process. The isolated-subprocess attempt that used to
            # precede this could never succeed -- the preparation stages hold the
            # parser registry's local closures, the database engine and the S3
            # client, none of which pickle -- so every parse paid for a failed spawn
            # attempt and then ran here anyway.
            prepared_stage = await heartbeat_while(
                self._runtime.stages.preparation.parse_and_normalize(
                    planned.request,
                    capture_stage,
                ),
                detail=heartbeat_detail,
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
                skip_reason=request.skip_reason,
            )
            self._observability.record_document_failure(
                planned.request.source_identity.connector_type.value,
            )
