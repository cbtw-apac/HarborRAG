"""Admission outcomes that bridge capture and durable release state."""

from harborrag_core.ingestion import (
    DocumentVersionSnapshot,
    SourceAdmissionDecision,
)
from harborrag_core.invariants import HarborInvariantError

from .models import DocumentReleaseRequest
from .stage_models import PreparedDocumentStage, RawCaptureStageResult


def active_candidate(
    *,
    request: DocumentReleaseRequest,
    capture: RawCaptureStageResult,
    active: DocumentVersionSnapshot | None,
    candidate_id: str,
    decision: SourceAdmissionDecision,
) -> PreparedDocumentStage:
    """Reuse the accepted artifact when force-repairing an active projection."""

    if not request.force_reprocess:
        return PreparedDocumentStage(
            document_id=capture.document_id,
            document_version_id=candidate_id,
            decision=decision,
        )
    if active is None or active.canonical_artifact is None:
        raise HarborInvariantError("active force-reprocess requires canonical artifacts")
    return PreparedDocumentStage(
        document_id=capture.document_id,
        document_version_id=candidate_id,
        decision=SourceAdmissionDecision.FORCE_REPROCESS,
        canonical_reference=active.canonical_artifact,
    )
