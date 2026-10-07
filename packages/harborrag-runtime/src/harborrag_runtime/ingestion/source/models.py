"""Source ingestion request, plan, summary, and outcome contracts."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    DocumentIngestionOutcome,
    IngestionTaskState,
    ProcessingProfile,
)
from harborrag_core.invariants import HarborInvariantError

from ..document.models import DocumentReleaseRequest
from ..limits import (
    validate_discovery_concurrency,
    validate_discovery_page_size,
    validate_document_concurrency,
)


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
        validate_document_concurrency(self.document_concurrency)
        validate_discovery_page_size(self.discovery_page_size)
        validate_discovery_concurrency(self.discovery_concurrency)
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

    def __post_init__(self) -> None:
        if min(self.published, self.unchanged, self.failed) < 0:
            raise ValueError("source dispatch counts must not be negative")

    @classmethod
    def from_results(
        cls,
        results: tuple[DocumentIngestionOutcome, ...],
    ) -> SourceDispatchSummary:
        if any(not isinstance(result, DocumentIngestionOutcome) for result in results):
            raise ValueError("unsupported document ingestion outcome")
        return cls(
            published=sum(result is DocumentIngestionOutcome.PUBLISHED for result in results),
            unchanged=sum(result is DocumentIngestionOutcome.UNCHANGED for result in results),
            failed=sum(result.is_failure for result in results),
        )

    @property
    def total(self) -> int:
        return self.published + self.unchanged + self.failed

    def require_total(self, expected: int) -> None:
        if self.total != expected:
            raise HarborInvariantError(
                "document outcome count does not match the source ingestion plan"
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
    # Empty for paged discovery: its documents already live in the persisted
    # pages, and holding every one of them here is what outgrew a worker.
    planned: tuple[PlannedDocumentRelease, ...]
    # Documents per persisted page, in page order; empty for connectors that
    # discover without pages (their plan is paged after the fact).
    page_counts: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedDocuments:
    """A dispatch plan walked one page at a time.

    Finalization and retry selection only ever read a plan front to back, so
    they take its pages instead of one tuple of every document: at hundreds of
    thousands of documents that tuple, and the JSON it was parsed from, no
    longer fit beside the rest of a worker.
    """

    document_count: int
    # Takes the page to start from, so a resumed walk skips what it already read.
    read_pages: Callable[[int], AsyncIterator[tuple[PlannedDocumentRelease, ...]]]

    def pages(self, start: int = 0) -> AsyncIterator[tuple[PlannedDocumentRelease, ...]]:
        return self.read_pages(start)

    @classmethod
    def of(cls, planned: tuple[PlannedDocumentRelease, ...]) -> PlannedDocuments:
        """An in-memory plan, for discoveries small enough to have been held whole."""

        async def pages(start: int) -> AsyncIterator[tuple[PlannedDocumentRelease, ...]]:
            if planned and start == 0:
                yield planned

        return cls(document_count=len(planned), read_pages=pages)


@dataclass(slots=True)
class RelationRepairProgress:
    """How far relation repair has walked a plan, and what it found on the way.

    Repair over a large source takes hours, and without this a retry of
    finalization started it again from the first page. The activity heartbeats
    this object, so the next attempt resumes at ``next_page`` with the totals
    of the pages it skips.
    """

    next_page: int = 0
    repaired_documents: int = 0
    resolved_relations: int = 0
    unresolved_relations: int = 0

    @classmethod
    def resume(cls, detail: object) -> RelationRepairProgress:
        """Progress from a prior attempt's heartbeat, or from the start when there is none."""

        if not isinstance(detail, dict):
            return cls()
        values = {name: detail.get(name) for name in cls.__slots__}
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in values.values()
        ):
            return cls()
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SourcePlanPageRange:
    page_number: int
    start_index: int
    count: int


@dataclass(frozen=True, slots=True)
class SourcePlanIndex:
    """Which persisted page holds which document indexes of a dispatch plan.

    A document workflow carries only its index into the plan. Without this a
    worker had to download and parse the whole plan to pick one record, which
    is quadratic in the plan size: at a few hundred documents invisible, at
    hundreds of thousands the run cannot finish.
    """

    document_count: int
    pages: tuple[SourcePlanPageRange, ...]

    def __post_init__(self) -> None:
        expected = 0
        for page in self.pages:
            if page.start_index != expected or page.count < 0:
                raise ValueError("source plan index pages must be contiguous")
            expected += page.count
        if expected != self.document_count:
            raise ValueError("source plan index pages must cover the document count")

    @classmethod
    def from_page_counts(cls, counts: Sequence[int]) -> SourcePlanIndex:
        pages = []
        start = 0
        for number, count in enumerate(counts):
            pages.append(SourcePlanPageRange(page_number=number, start_index=start, count=count))
            start += count
        return cls(document_count=start, pages=tuple(pages))

    def locate(self, document_index: int) -> tuple[int, int]:
        """``(page_number, offset within page)`` for one document index."""

        if document_index < 0 or document_index >= self.document_count:
            raise IndexError(document_index)
        starts = [page.start_index for page in self.pages]
        position = bisect_right(starts, document_index) - 1
        page = self.pages[position]
        return page.page_number, document_index - page.start_index


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
