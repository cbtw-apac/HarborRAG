"""Source admission, raw capture, and canonical candidate stages."""

from __future__ import annotations

import asyncio
import logging

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    ChangeFingerprintBuilder,
    DocumentIdentityBuilder,
    DocumentVersionCandidate,
    DocumentVersionState,
    RawDocumentReference,
    SourceAdmissionDecision,
)
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion import (
    CanonicalVersionPlanner,
    SourceAdmissionPolicy,
)

from .dependencies import DocumentReleaseDependencies
from .lifecycle import DocumentVersionLifecycle
from .materialization_helpers import enrich_raw_document
from .models import DocumentReleaseRequest
from .stage_models import PreparedDocumentStage, RawCaptureStageResult

logger = logging.getLogger("harborrag.runtime.ingestion.capture")


class DocumentCaptureStages:
    """Own source admission, raw capture, and canonical candidate creation."""

    def __init__(self, dependencies: DocumentReleaseDependencies) -> None:
        self._dependencies = dependencies
        self._identities = DocumentIdentityBuilder()
        self._fingerprints = ChangeFingerprintBuilder()
        self._admission = SourceAdmissionPolicy()
        self._planner = CanonicalVersionPlanner()
        self._lifecycle = DocumentVersionLifecycle(
            control=dependencies.control,
            canonical_artifacts=dependencies.canonical_artifacts,
            chunk_reader=dependencies.chunk_reader,
            projection_artifacts=dependencies.projection_artifacts,
        )

    async def fetch_and_capture(
        self,
        request: DocumentReleaseRequest,
        connector: BaseConnector | HarborConnector,
    ) -> RawCaptureStageResult:
        document_id = self._identities.document_id(
            tenant_id=request.tenant_id,
            connector_type=request.source_identity.connector_type,
            connection_id=request.source_identity.connection_id,
            source_item_id=request.source_identity.source_item_id,
        )
        active = await self._dependencies.control.document_versions.active_snapshot(document_id)
        decision = self._admission.before_fetch(
            active=active,
            admission_change_key=self._fingerprints.admission_change_key(
                snapshot=request.admission
            ),
            processing_fingerprint=self._fingerprints.processing_fingerprint(
                profile=request.processing
            ),
            force_reprocess=request.force_reprocess,
        )
        if request.discovery_decision in {
            SourceAdmissionDecision.NEW,
            SourceAdmissionDecision.UPDATED,
            SourceAdmissionDecision.METADATA_CHANGED,
            SourceAdmissionDecision.FORCE_REPROCESS,
        }:
            decision = request.discovery_decision
        if decision == SourceAdmissionDecision.UNCHANGED:
            return RawCaptureStageResult(
                document_id=document_id,
                document_version_id=(
                    str(active.document_version_id) if active is not None else None
                ),
                decision=decision,
            )
        raw = await asyncio.to_thread(connector.load, request.source)
        raw = enrich_raw_document(raw, request)
        reference = await self._dependencies.raw_artifacts.put(
            connector=request.source_identity.connector_type,
            document_id=document_id,
            document=raw,
            context=_context(request.tenant_id),
        )
        return RawCaptureStageResult(
            document_id=document_id,
            document_version_id=None,
            decision=decision,
            raw_reference=reference,
        )

    async def parse_and_normalize(
        self,
        request: DocumentReleaseRequest,
        capture: RawCaptureStageResult,
    ) -> PreparedDocumentStage:
        if capture.raw_reference is None:
            if capture.document_version_id is None:
                raise HarborInvariantError("capture.document_version_id must not be None here")
            return PreparedDocumentStage(
                document_id=capture.document_id,
                document_version_id=capture.document_version_id,
                decision=capture.decision,
            )
        context = _context(request.tenant_id)
        candidate_id: str | None = None
        try:
            raw = await self._dependencies.raw_artifacts.get(
                capture.raw_reference,
                context=context,
            )
            parsed = await asyncio.to_thread(
                self._dependencies.parser.parse,
                raw,
            )
            normalized = await asyncio.to_thread(
                self._dependencies.normalizer.normalize,
                raw,
                parsed,
            )
            planned = self._planner.plan(
                document=normalized,
                source_identity=request.source_identity,
                admission=request.admission,
                processing=request.processing,
            )
            candidate_id = str(planned.candidate.document_version_id)
            if not self._has_indexable_content(normalized):
                logger.info(
                    "Normalized document skipped because it has no indexable content "
                    "document_id=%s document_version_id=%s connector=%s",
                    capture.document_id,
                    candidate_id,
                    request.source_identity.connector_type.value,
                )
                return PreparedDocumentStage(
                    document_id=capture.document_id,
                    document_version_id=candidate_id,
                    decision=SourceAdmissionDecision.UNSUPPORTED,
                )
            active = await self._dependencies.control.document_versions.active_snapshot(
                capture.document_id
            )
            decision = self._admission.after_normalization(
                active=active,
                fingerprints=planned.candidate.fingerprints,
            )
            if decision == SourceAdmissionDecision.UNCHANGED and not request.force_reprocess:
                if active is None:
                    raise HarborInvariantError("active must not be None here")
                return PreparedDocumentStage(
                    document_id=capture.document_id,
                    document_version_id=str(active.document_version_id),
                    decision=decision,
                )
            current = await self._dependencies.control.document_versions.create_candidate(
                planned.candidate
            )
            if current == DocumentVersionState.ACTIVE:
                return PreparedDocumentStage(
                    document_id=capture.document_id,
                    document_version_id=candidate_id,
                    decision=decision,
                )
            document, snapshot = await self._lifecycle.materialize_document(
                document_version_id=candidate_id,
                current=current,
                candidate=planned.document,
                context=context,
            )
            await self._lifecycle.record_raw(
                candidate_id,
                capture.raw_reference,
            )
            canonical_reference = snapshot.canonical_artifact
            if canonical_reference is None:
                canonical_reference = await self._dependencies.canonical_artifacts.put(
                    document_id=capture.document_id,
                    document_version_id=candidate_id,
                    document=document,
                    context=context,
                )
            return PreparedDocumentStage(
                document_id=capture.document_id,
                document_version_id=candidate_id,
                decision=decision,
                canonical_reference=canonical_reference,
            )
        except Exception as error:
            if candidate_id is not None:
                await self._lifecycle.record_failure(
                    document_id=capture.document_id,
                    document_version_id=candidate_id,
                    stage="ParseAndNormalize",
                    error=error,
                )
            raise

    @staticmethod
    def _has_indexable_content(document: Document) -> bool:
        return bool(document.table_artifacts) or any(
            element.content is not None and element.content.strip() for element in document.content
        )

    async def prepare_canonical(
        self,
        *,
        tenant_id: str,
        candidate: DocumentVersionCandidate,
        document: Document,
        decision: SourceAdmissionDecision,
    ) -> PreparedDocumentStage:
        """Start a connector-free release at its canonical replay boundary."""

        document_id = str(candidate.document_id)
        document_version_id = str(candidate.document_version_id)
        try:
            current = await self._dependencies.control.document_versions.create_candidate(candidate)
            if current == DocumentVersionState.ACTIVE:
                return PreparedDocumentStage(
                    document_id=document_id,
                    document_version_id=document_version_id,
                    decision=decision,
                )
            context = _context(tenant_id)
            materialized, snapshot = await self._lifecycle.materialize_document(
                document_version_id=document_version_id,
                current=current,
                candidate=document,
                context=context,
            )
            reference = snapshot.canonical_artifact
            if reference is None:
                reference = await self._dependencies.canonical_artifacts.put(
                    document_id=document_id,
                    document_version_id=document_version_id,
                    document=materialized,
                    context=context,
                )
            return PreparedDocumentStage(
                document_id=document_id,
                document_version_id=document_version_id,
                decision=decision,
                canonical_reference=reference,
            )
        except Exception as error:
            await self._lifecycle.record_failure(
                document_id=document_id,
                document_version_id=document_version_id,
                stage="PersistCanonical",
                error=error,
            )
            raise

    async def replay_from_artifacts(
        self,
        request: DocumentReleaseRequest,
        document_version_id: str,
    ) -> PreparedDocumentStage:
        """Resume a durable version without contacting its source connector."""

        snapshot = await self._dependencies.control.document_versions.get_version(
            document_version_id
        )
        if snapshot is None:
            raise ValueError(f"unknown document version: {document_version_id}")
        expected_document_id = self._identities.document_id(
            tenant_id=request.tenant_id,
            connector_type=request.source_identity.connector_type,
            connection_id=request.source_identity.connection_id,
            source_item_id=request.source_identity.source_item_id,
        )
        if snapshot.document_id != expected_document_id:
            raise ValueError("replay request does not own the selected document version")
        if snapshot.state == DocumentVersionState.ACTIVE:
            return PreparedDocumentStage(
                document_id=str(snapshot.document_id),
                document_version_id=document_version_id,
                decision=SourceAdmissionDecision.UNCHANGED,
            )

        await self._lifecycle.prepare_replay(document_version_id, snapshot.state)
        snapshot = await self._dependencies.control.document_versions.get_version(
            document_version_id
        )
        if snapshot is None:
            raise HarborInvariantError("snapshot must not be None here")
        if snapshot.canonical_artifact is not None:
            return PreparedDocumentStage(
                document_id=str(snapshot.document_id),
                document_version_id=document_version_id,
                decision=SourceAdmissionDecision.FORCE_REPROCESS,
                canonical_reference=snapshot.canonical_artifact,
            )
        if snapshot.raw_artifact is None or snapshot.raw_metadata_artifact is None:
            raise RuntimeError("document version has no reusable raw or canonical artifact")
        raw_reference = RawDocumentReference(
            document_id=snapshot.document_id,
            connector_type=request.source_identity.connector_type.value,
            content_hash=snapshot.raw_artifact.sha256,
            source_artifact=snapshot.raw_artifact,
            metadata_artifact=snapshot.raw_metadata_artifact,
        )
        return await self.parse_and_normalize(
            request,
            RawCaptureStageResult(
                document_id=str(snapshot.document_id),
                document_version_id=None,
                decision=SourceAdmissionDecision.FORCE_REPROCESS,
                raw_reference=raw_reference,
            ),
        )


def _context(tenant_id: str) -> StorageOperationContext:
    return StorageOperationContext.system(tenant_id)
