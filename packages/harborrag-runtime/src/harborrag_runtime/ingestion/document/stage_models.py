"""Value objects passed between document release stages."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ingestion import (
    ArtifactReference,
    DocumentVersionSnapshot,
    RawDocumentReference,
    SourceAdmissionDecision,
)


@dataclass(frozen=True, slots=True)
class RawCaptureStageResult:
    """Small durable output from admission and immutable raw capture."""

    document_id: str
    document_version_id: str | None
    decision: SourceAdmissionDecision
    raw_reference: RawDocumentReference | None = None

    @property
    def unchanged(self) -> bool:
        return self.raw_reference is None

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("raw capture document ID must be non-empty")
        if self.unchanged and self.document_version_id is None:
            raise ValueError("an unchanged raw-capture result requires an active version")


@dataclass(frozen=True, slots=True)
class PreparedDocumentStage:
    """Canonical candidate identity passed between Temporal stage activities."""

    document_id: str
    document_version_id: str
    decision: SourceAdmissionDecision
    canonical_reference: ArtifactReference | None = None
    # The active version admission read when it decided to build this candidate;
    # publication is compare-and-set on it. ``None`` with the flag set means no
    # version was active. The flag is False only for payloads recorded before the
    # base existed: publication then falls back to the pointer it reads itself.
    expected_active_document_version_id: str | None = None
    publication_base_known: bool = False

    @property
    def requires_processing(self) -> bool:
        return self.canonical_reference is not None

    def __post_init__(self) -> None:
        if not self.document_id.strip() or not self.document_version_id.strip():
            raise ValueError("prepared document identities must be non-empty")


def active_version_id(active: DocumentVersionSnapshot | None) -> str | None:
    return str(active.document_version_id) if active is not None else None


def publication_base(
    prepared: PreparedDocumentStage,
    current: DocumentVersionSnapshot | None,
) -> str | None:
    """The base to publish against: the recorded one, else today's pointer."""

    if prepared.publication_base_known:
        return prepared.expected_active_document_version_id
    return active_version_id(current)
