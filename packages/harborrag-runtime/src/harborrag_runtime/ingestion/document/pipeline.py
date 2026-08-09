"""Ordered document release pipeline."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_runtime.document_stage_catalog import DOCUMENT_STAGE_CATALOG

from .dependencies import DocumentReleaseDependencies
from .models import DocumentReleaseOutcome, DocumentReleaseRequest
from .preparation import DocumentPreparationStages
from .projection import DocumentProjectionStages
from .stage_models import PreparedDocumentStage

logger = logging.getLogger("harborrag.runtime.ingestion.document_pipeline")


class DocumentStagePipeline:
    """Compose named, independently retryable document ingestion stages."""

    def __init__(self, dependencies: DocumentReleaseDependencies) -> None:
        self.preparation = DocumentPreparationStages(dependencies)
        self.projections = DocumentProjectionStages(dependencies)

    async def release(
        self,
        request: DocumentReleaseRequest,
        connector: BaseConnector | HarborConnector,
    ) -> DocumentReleaseOutcome:
        started_at = perf_counter()
        connector_type = request.source_identity.connector_type.value
        logger.debug(
            "Document preparation started connector_type=%s binding_kind=%s",
            connector_type,
            request.source_identity.binding.kind.value,
        )
        try:
            capture = await self.preparation.fetch_and_capture(
                request,
                connector,
            )
        except Exception as error:
            logger.error(
                "Document stage failed stage=FetchAndCaptureRaw connector_type=%s "
                "error_type=%s duration_ms=%.1f",
                connector_type,
                type(error).__name__,
                (perf_counter() - started_at) * 1000,
            )
            raise
        logger.debug(
            "Document stage completed stage=FetchAndCaptureRaw document_id=%s "
            "decision=%s duration_ms=%.1f",
            capture.document_id,
            capture.decision.value,
            (perf_counter() - started_at) * 1000,
        )

        parse_started_at = perf_counter()
        try:
            prepared = await self.preparation.parse_and_normalize(
                request,
                capture,
            )
        except Exception as error:
            logger.error(
                "Document stage failed stage=ParseAndNormalize document_id=%s "
                "error_type=%s duration_ms=%.1f",
                capture.document_id,
                type(error).__name__,
                (perf_counter() - parse_started_at) * 1000,
            )
            raise
        logger.debug(
            "Document stage completed stage=ParseAndNormalize document_id=%s "
            "document_version_id=%s duration_ms=%.1f",
            prepared.document_id,
            prepared.document_version_id,
            (perf_counter() - parse_started_at) * 1000,
        )
        return await self.release_prepared(request, prepared)

    async def replay(
        self,
        request: DocumentReleaseRequest,
        document_version_id: str,
    ) -> DocumentReleaseOutcome:
        """Resume from the earliest durable artifact; never fetch the connector."""

        prepared = await self.preparation.replay_from_artifacts(
            request,
            document_version_id,
        )
        logger.info(
            "Document replay prepared document_id=%s document_version_id=%s",
            prepared.document_id,
            prepared.document_version_id,
        )
        return await self.release_prepared(request, prepared)

    async def release_prepared(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> DocumentReleaseOutcome:
        """Resume the shared pipeline from a canonical candidate reference."""

        if not prepared.requires_processing:
            outcome = await self.projections.publish_version(prepared)
            self._log_outcome(outcome)
            return outcome
        stage_functions: dict[
            str,
            Callable[[DocumentReleaseRequest, PreparedDocumentStage], Awaitable[object]],
        ] = {
            "SyncContentUnits": self.preparation.sync_content_units,
            "PersistCanonical": self.preparation.persist_canonical,
            "ChunkAndValidate": self.preparation.chunk_and_validate,
            "EncodeChunks": self.preparation.encode_chunks,
            "BuildRelations": self.projections.build_relations,
            "BuildProjections": self.projections.build_projections,
            "WriteVectorProjection": self.projections.write_vector_projection,
            "WriteGraphProjection": self.projections.write_graph_projection,
            "VerifyProjections": self.projections.verify_projections,
        }
        stages = tuple((spec.name, stage_functions[spec.name]) for spec in DOCUMENT_STAGE_CATALOG)
        for stage_name, stage in stages:
            started_at = perf_counter()
            logger.debug(
                "Document stage started document_id=%s document_version_id=%s stage=%s",
                prepared.document_id,
                prepared.document_version_id,
                stage_name,
            )
            try:
                await stage(request, prepared)
            except Exception as error:
                logger.error(
                    "Document stage failed document_id=%s document_version_id=%s "
                    "stage=%s error_type=%s duration_ms=%.1f",
                    prepared.document_id,
                    prepared.document_version_id,
                    stage_name,
                    type(error).__name__,
                    (perf_counter() - started_at) * 1000,
                )
                await self.preparation.record_failure(
                    prepared,
                    stage=stage_name,
                    error=error,
                )
                raise
            logger.debug(
                "Document stage completed document_id=%s document_version_id=%s "
                "stage=%s duration_ms=%.1f",
                prepared.document_id,
                prepared.document_version_id,
                stage_name,
                (perf_counter() - started_at) * 1000,
            )
        publish_started_at = perf_counter()
        try:
            outcome = await self.projections.publish_version(prepared)
        except Exception as error:
            logger.error(
                "Document stage failed document_id=%s document_version_id=%s "
                "stage=PublishVersion error_type=%s duration_ms=%.1f",
                prepared.document_id,
                prepared.document_version_id,
                type(error).__name__,
                (perf_counter() - publish_started_at) * 1000,
            )
            await self.preparation.record_failure(
                prepared,
                stage="PublishVersion",
                error=error,
            )
            raise
        logger.debug(
            "Document stage completed document_id=%s document_version_id=%s "
            "stage=PublishVersion duration_ms=%.1f",
            prepared.document_id,
            prepared.document_version_id,
            (perf_counter() - publish_started_at) * 1000,
        )
        self._log_outcome(outcome)
        return outcome

    @staticmethod
    def _log_outcome(outcome: DocumentReleaseOutcome) -> None:
        logger.info(
            "Document pipeline completed document_id=%s document_version_id=%s "
            "decision=%s published=%s evidence_chunks=%d "
            "graph_nodes=%d graph_relations=%d",
            outcome.document_id,
            outcome.document_version_id,
            outcome.decision.value,
            outcome.published,
            outcome.evidence_chunks,
            outcome.graph_nodes,
            outcome.graph_relations,
        )
