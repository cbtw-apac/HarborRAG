"""Value objects passed between document release stages."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ingestion import (
    ArtifactReference,
    RawDocumentReference,
    SourceAdmissionDecision,
)


@dataclass(frozen=True, slots=True)
class RawCaptureStageResult:
    """Small durable output from admission and immutable raw capture.

    Args:
        document_id: Document the capture belongs to.
        document_version_id: Candidate version, or the active one when unchanged.
        decision: Admission decision for the source version.
        raw_reference: Captured raw artifact; ``None`` when the source is unchanged.

    Raises:
        ValueError: ``document_id`` is blank, or an unchanged result has no version.
    """

    document_id: str
    document_version_id: str | None
    decision: SourceAdmissionDecision
    raw_reference: RawDocumentReference | None = None

    @property
    def unchanged(self) -> bool:
        """Report whether nothing new was captured."""
        return self.raw_reference is None

    def __post_init__(self) -> None:
        """Validate the capture identity."""
        if not self.document_id.strip():
            raise ValueError("raw capture document ID must be non-empty")
        if self.unchanged and self.document_version_id is None:
            raise ValueError("an unchanged raw-capture result requires an active version")


@dataclass(frozen=True, slots=True)
class PreparedDocumentStage:
    """Canonical candidate identity passed between Temporal stage activities.

    Args:
        document_id: Document the candidate belongs to.
        document_version_id: Candidate version.
        decision: Admission decision for the source version.
        canonical_reference: Canonical artifact; ``None`` when nothing needs processing.

    Raises:
        ValueError: A document or version identity is blank.
    """

    document_id: str
    document_version_id: str
    decision: SourceAdmissionDecision
    canonical_reference: ArtifactReference | None = None

    @property
    def requires_processing(self) -> bool:
        """Report whether a canonical artifact awaits processing."""
        return self.canonical_reference is not None

    def __post_init__(self) -> None:
        """Validate the candidate identity."""
        if not self.document_id.strip() or not self.document_version_id.strip():
            raise ValueError("prepared document identities must be non-empty")
