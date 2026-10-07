"""Admission outcomes that bridge capture and durable release state."""

from harborrag_core.ingestion import (
    DocumentVersionSnapshot,
    SourceAdmissionDecision,
)
from harborrag_core.invariants import HarborInvariantError

from .models import DocumentReleaseRequest
from .stage_models import PreparedDocumentStage, RawCaptureStageResult, active_version_id


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
            expected_active_document_version_id=active_version_id(active),
            publication_base_known=True,
        )
    if active is None or active.canonical_artifact is None:
        raise HarborInvariantError("active force-reprocess requires canonical artifacts")
    return PreparedDocumentStage(
        document_id=capture.document_id,
        document_version_id=candidate_id,
        decision=SourceAdmissionDecision.FORCE_REPROCESS,
        canonical_reference=active.canonical_artifact,
        expected_active_document_version_id=active_version_id(active),
        publication_base_known=True,
    )


def overtaken_by_newer_publish(
    replayed: DocumentVersionSnapshot,
    active: DocumentVersionSnapshot | None,
) -> bool:
    """Whether another version was published after ``replayed`` was created.

    A failed version is replayed long after it was built; by then a newer release
    may stand, and publishing the replay would put older content back over it.
    Without both timestamps nothing is known, and the replay keeps today's pointer.
    """

    if active is None or active.document_version_id == replayed.document_version_id:
        return False
    if active.activated_at is None or replayed.created_at is None:
        return False
    return active.activated_at > replayed.created_at
