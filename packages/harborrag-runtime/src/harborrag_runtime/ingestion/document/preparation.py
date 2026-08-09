"""Facade over document capture and materialization stages."""

from __future__ import annotations

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    DocumentVersionCandidate,
    SourceAdmissionDecision,
)
from harborrag_engine.ingestion.chunking.schemas import ChunkingStatistics

from .capture import DocumentCaptureStages
from .dependencies import DocumentReleaseDependencies
from .materialization import DocumentMaterializationStages
from .models import DocumentReleaseRequest
from .stage_models import PreparedDocumentStage, RawCaptureStageResult


class DocumentPreparationStages:
    """Stable facade for source-capture and materialization stage families."""

    def __init__(self, dependencies: DocumentReleaseDependencies) -> None:
        self._capture = DocumentCaptureStages(dependencies)
        self._materialization = DocumentMaterializationStages(dependencies)

    async def fetch_and_capture(
        self,
        request: DocumentReleaseRequest,
        connector: BaseConnector | HarborConnector,
    ) -> RawCaptureStageResult:
        return await self._capture.fetch_and_capture(request, connector)

    async def parse_and_normalize(
        self,
        request: DocumentReleaseRequest,
        capture: RawCaptureStageResult,
    ) -> PreparedDocumentStage:
        return await self._capture.parse_and_normalize(request, capture)

    async def prepare_canonical(
        self,
        *,
        tenant_id: str,
        candidate: DocumentVersionCandidate,
        document: Document,
        decision: SourceAdmissionDecision,
    ) -> PreparedDocumentStage:
        return await self._capture.prepare_canonical(
            tenant_id=tenant_id,
            candidate=candidate,
            document=document,
            decision=decision,
        )

    async def replay_from_artifacts(
        self,
        request: DocumentReleaseRequest,
        document_version_id: str,
    ) -> PreparedDocumentStage:
        return await self._capture.replay_from_artifacts(request, document_version_id)

    async def sync_content_units(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        await self._materialization.sync_content_units(request, prepared)

    async def persist_canonical(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        await self._materialization.persist_canonical(request, prepared)

    async def chunk_and_validate(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> ChunkingStatistics | None:
        return await self._materialization.chunk_and_validate(request, prepared)

    async def encode_chunks(
        self,
        request: DocumentReleaseRequest,
        prepared: PreparedDocumentStage,
    ) -> None:
        await self._materialization.encode_chunks(request, prepared)

    async def record_failure(
        self,
        prepared: PreparedDocumentStage,
        *,
        stage: str,
        error: Exception,
    ) -> None:
        await self._materialization.record_failure(
            prepared,
            stage=stage,
            error=error,
        )
