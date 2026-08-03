"""Source ingestion request, plan, summary, and outcome contracts."""

from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import IngestionTaskState, ProcessingProfile

from ..document.models import DocumentReleaseRequest


@dataclass(frozen=True, slots=True)
class SourceIngestionRequest:
    tenant_id: str
    task_id: str
    connector_name: str
    connector_type: ConnectorType
    connection_id: str
    source_scope_id: str
    configuration_fingerprint: str
    processing: ProcessingProfile
    query: ConnectorQuery = field(default_factory=ConnectorQuery)
    force_reprocess: bool = False
    discovery_page_size: int = 50
    discovery_concurrency: int = 4
    document_concurrency: int = 8
    missing_threshold: int = 2

    def __post_init__(self) -> None:
        text_values = (
            self.tenant_id,
            self.task_id,
            self.connector_name,
            self.connection_id,
            self.source_scope_id,
            self.configuration_fingerprint,
        )
        if any(not value.strip() for value in text_values):
            raise ValueError("source ingestion identity values must be non-empty")
        if not 1 <= self.document_concurrency <= 100:
            raise ValueError("document_concurrency must be between 1 and 100")
        if not 1 <= self.discovery_page_size <= 300:
            raise ValueError("discovery_page_size must be between 1 and 300")
        if not 1 <= self.discovery_concurrency <= 32:
            raise ValueError("discovery_concurrency must be between 1 and 32")
        if self.missing_threshold < 1:
            raise ValueError("missing_threshold must be positive")


@dataclass(frozen=True, slots=True)
class SourceIngestionOutcome:
    task_id: str
    scan_id: str
    discovered: int
    published: int
    unchanged: int
    failed: int
    status: IngestionTaskState
    removal_candidates: tuple[str, ...] = ()
    unresolved_relations: int = 0


@dataclass(frozen=True, slots=True)
class SourceDispatchSummary:
    published: int = 0
    unchanged: int = 0
    failed: int = 0

    @classmethod
    def from_results(
        cls,
        results: tuple[str, ...],
    ) -> SourceDispatchSummary:
        return cls(
            published=sum(result == "published" for result in results),
            unchanged=sum(result == "unchanged" for result in results),
            failed=sum(result == "failed" for result in results),
        )

    def merge(self, other: SourceDispatchSummary) -> SourceDispatchSummary:
        return SourceDispatchSummary(
            published=self.published + other.published,
            unchanged=self.unchanged + other.unchanged,
            failed=self.failed + other.failed,
        )

    def task_state(self) -> IngestionTaskState:
        """Derive the terminal task state from bounded document outcomes."""

        completed = self.published + self.unchanged
        if self.failed and completed:
            return IngestionTaskState.PARTIAL
        if self.failed:
            return IngestionTaskState.FAILED
        return IngestionTaskState.COMPLETED


@dataclass(frozen=True, slots=True)
class PlannedDocumentRelease:
    request: DocumentReleaseRequest
    document_id: str


@dataclass(frozen=True, slots=True)
class SourceDiscoveryRun:
    scan_id: str
    planned: tuple[PlannedDocumentRelease, ...]


@dataclass(frozen=True, slots=True)
class SourceDiscoveryPage:
    planned: tuple[PlannedDocumentRelease, ...]
    next_cursor: str | None
    root_count: int
    provider_seconds: float = 0.0
    descriptor_seconds: float = 0.0

    def __post_init__(self) -> None:
        _validate_discovery_cursor(self.next_cursor)
        if self.root_count < 0:
            raise ValueError("source discovery root count must not be negative")


@dataclass(frozen=True, slots=True)
class SourcePlanCheckpoint:
    planned: tuple[PlannedDocumentRelease, ...]
    next_cursor: str | None
    root_count: int

    def __post_init__(self) -> None:
        _validate_discovery_cursor(self.next_cursor)
        if self.root_count < 0:
            raise ValueError("source plan checkpoint root count must not be negative")


def _validate_discovery_cursor(cursor: str | None) -> None:
    if cursor is None:
        return
    if not cursor or len(cursor) > 4096 or any(ord(character) < 32 for character in cursor):
        raise ValueError("source discovery cursor is invalid")
