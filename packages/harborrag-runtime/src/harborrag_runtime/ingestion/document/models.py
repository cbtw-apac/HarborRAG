"""Document release request and outcome contracts."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ProcessingProfile,
    PublicationResult,
    SourceAdmissionDecision,
    SourceIdentity,
)


@dataclass(frozen=True, slots=True)
class DocumentReleaseRequest:
    tenant_id: str
    connector_name: str
    source: SourceRecord
    source_identity: SourceIdentity
    admission: AdmissionSnapshot
    processing: ProcessingProfile
    configuration_fingerprint: str | None = None
    discovery_decision: SourceAdmissionDecision | None = None
    force_reprocess: bool = False

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.connector_name.strip():
            raise ValueError("release tenant and connector names must be non-empty")
        if (
            self.configuration_fingerprint is not None
            and not self.configuration_fingerprint.strip()
        ):
            raise ValueError("release configuration fingerprint must be non-empty")
        if self.source_identity.tenant_id != self.tenant_id:
            raise ValueError("release tenant must match its source identity tenant")


@dataclass(frozen=True, slots=True)
class DocumentReleaseOutcome:
    document_id: str
    document_version_id: str | None
    decision: SourceAdmissionDecision
    evidence_chunks: int = 0
    graph_nodes: int = 0
    graph_relations: int = 0
    publication: PublicationResult | None = None

    @property
    def published(self) -> bool:
        return self.publication is not None
