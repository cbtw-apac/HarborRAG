"""Source admission, raw capture, and canonical candidate stages."""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    ArtifactReference,
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
    produces_evidence,
    with_title_as_content,
)

from .capture_decisions import active_candidate
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
        # Parsing is the longest document stage, so each step's share is logged
        # per document: it tells a slow parser from slow storage on a large run.
        marks = [perf_counter()]
        try:
            raw = await self._dependencies.raw_artifacts.get(
                capture.raw_reference,
                context=context,
            )
            marks.append(perf_counter())
            parsed = await asyncio.to_thread(
                self._dependencies.parser.parse,
                raw,
            )
            marks.append(perf_counter())
            normalized = await asyncio.to_thread(
                self._dependencies.normalizer.normalize,
                raw,
                parsed,
            )
            marks.append(perf_counter())
            normalized = with_title_as_content(
                normalized,
                binding=request.source_identity.binding.kind,
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
            marks.append(perf_counter())
            if decision == SourceAdmissionDecision.UNCHANGED and not request.force_reprocess:
                if active is None:
                    raise HarborInvariantError("active must not be None here")
                _log_parse_timings(capture.document_id, candidate_id, decision, marks)
                return PreparedDocumentStage(
                    document_id=capture.document_id,
                    document_version_id=str(active.document_version_id),
                    decision=decision,
                )
            current = await self._dependencies.control.document_versions.create_candidate(
                planned.candidate
            )
            if current == DocumentVersionState.ACTIVE:
                return active_candidate(
                    request=request,
                    capture=capture,
                    active=active,
                    candidate_id=candidate_id,
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
                canonical_reference = await self._record_canonical(
                    document_id=capture.document_id,
                    document_version_id=candidate_id,
                    document=document,
                    context=context,
                )
            marks.append(perf_counter())
            _log_parse_timings(capture.document_id, candidate_id, decision, marks)
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

    async def _record_canonical(
        self,
        *,
        document_id: str,
        document_version_id: str,
        document: Document,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        """Write the canonical boundary and record it in the same activity.

        Recording it only in the later PersistCanonical activity left a window in
        which a retried parse rebuilt the document and met the object the earlier
        attempt had written. Parsing is not byte-for-byte reproducible (OCR under
        load), so that retry failed permanently on the immutable key. The caller
        only gets here when no reference is recorded, so the first writer's object
        is adopted: the version id already pins its evidence and retrieval content.
        """

        reference = await self._dependencies.canonical_artifacts.put(
            document_id=document_id,
            document_version_id=document_version_id,
            document=document,
            context=context,
            adopt_existing=True,
        )
        await self._lifecycle.advance(
            document_version_id,
            DocumentVersionState.CANONICAL_READY,
            artifact_column="canonical_artifact",
            artifact=reference,
        )
        return reference

    @staticmethod
    def _has_indexable_content(document: Document) -> bool:
        """Admit only what the chunker will turn into evidence.

        Counting any non-empty element let a heading-only document through, and the
        vector projection then rejected the evidence-free batch -- turning a page that
        should have been a clean UNSUPPORTED skip into a failed release. Segmentation
        owns the rule.
        """

        return produces_evidence(document)

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
                reference = await self._record_canonical(
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


_PARSE_STEPS = ("raw", "parse", "normalize", "admit", "persist")


def _log_parse_timings(
    document_id: str, document_version_id: str, decision: object, marks: list[float]
) -> None:
    """One line per parsed document with the milliseconds each step took."""

    steps = " ".join(
        f"{name}_ms={(marks[index + 1] - marks[index]) * 1000:.0f}"
        for index, name in enumerate(_PARSE_STEPS)
        if index + 1 < len(marks)
    )
    logger.info(
        "Document parsed document_id=%s document_version_id=%s decision=%s %s total_ms=%.0f",
        document_id,
        document_version_id,
        getattr(decision, "value", decision),
        steps,
        (marks[-1] - marks[0]) * 1000,
    )
