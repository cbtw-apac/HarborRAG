from __future__ import annotations

from dataclasses import dataclass

from harborrag_runtime.temporal.source_query import ProcessingProfileInput, SourceQuery

_SOURCE_TASK_STATES = frozenset(
    "PENDING RUNNING PAUSED CANCELLING COMPLETED PARTIAL FAILED CANCELLED".split()
)


@dataclass(frozen=True, slots=True)
class WorkflowArtifactReference:
    bucket: str
    key: str
    sha256: str
    byte_size: int
    media_type: str
    byte_offset: int | None = None
    byte_length: int | None = None

    def __post_init__(self) -> None:
        if not self.bucket.strip() or not self.key.strip():
            raise ValueError("artifact bucket and key must be non-empty")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("artifact sha256 must be lowercase hexadecimal")
        if not self.media_type.strip():
            raise ValueError("artifact media_type must be non-empty")
        if self.byte_size < 0:
            raise ValueError("artifact byte_size must not be negative")
        if (self.byte_offset is None) != (self.byte_length is None):
            raise ValueError("artifact range values must be set together")
        if self.byte_offset is not None:
            assert self.byte_length is not None
            if self.byte_offset < 0 or self.byte_length < 0:
                raise ValueError("artifact range values must not be negative")
            if self.byte_offset + self.byte_length > self.byte_size:
                raise ValueError("artifact range exceeds the artifact size")


@dataclass(frozen=True, slots=True)
class DocumentDispatchSummary:
    published: int = 0
    unchanged: int = 0
    failed: int = 0

    def add(self, status: str) -> DocumentDispatchSummary:
        return DocumentDispatchSummary(
            published=self.published + (status == "published"),
            unchanged=self.unchanged + (status == "unchanged"),
            failed=self.failed + (status == "failed"),
        )

    def merge(self, other: DocumentDispatchSummary) -> DocumentDispatchSummary:
        return DocumentDispatchSummary(
            published=self.published + other.published,
            unchanged=self.unchanged + other.unchanged,
            failed=self.failed + other.failed,
        )


@dataclass(frozen=True, slots=True)
class SourceContinuation:
    """Small durable cursor carried across bounded workflow histories."""

    scan_id: str
    plan_reference: WorkflowArtifactReference
    document_count: int
    next_document_index: int
    batch_number: int
    summary: DocumentDispatchSummary

    def __post_init__(self) -> None:
        if not self.scan_id.strip():
            raise ValueError("continued source scan ID must be non-empty")
        if not 0 <= self.next_document_index <= self.document_count:
            raise ValueError("continued source document cursor is invalid")
        if self.document_count < 0 or self.batch_number < 0:
            raise ValueError("continued source counters must not be negative")


@dataclass(frozen=True, slots=True)
class SourceIngestionInput:
    task_id: str
    tenant_id: str
    connector_name: str
    connector_type: str
    connection_id: str
    source_scope_id: str
    configuration_fingerprint: str
    processing: ProcessingProfileInput
    query: SourceQuery = SourceQuery()
    force_reprocess: bool = False
    discovery_page_size: int = 50
    discovery_concurrency: int = 4
    document_concurrency: int = 8
    missing_threshold: int = 2
    batch_size: int = 200
    continue_after_batches: int = 25
    continuation: SourceContinuation | None = None

    def __post_init__(self) -> None:
        values = (
            self.task_id,
            self.tenant_id,
            self.connector_name,
            self.connector_type,
            self.connection_id,
            self.source_scope_id,
            self.configuration_fingerprint,
        )
        if any(not value.strip() for value in values):
            raise ValueError("source input identities must be non-empty")
        if len(self.task_id) > 128:
            raise ValueError("source task ID must not exceed 128 characters")
        if not 1 <= self.batch_size <= 300:
            raise ValueError("source batch_size must be between 1 and 300")
        if not 1 <= self.document_concurrency <= 100:
            raise ValueError("document_concurrency must be between 1 and 100")
        if not 1 <= self.discovery_page_size <= 300:
            raise ValueError("discovery_page_size must be between 1 and 300")
        if not 1 <= self.discovery_concurrency <= 32:
            raise ValueError("discovery_concurrency must be between 1 and 32")
        if self.missing_threshold < 1:
            raise ValueError("missing_threshold must be positive")
        if not 1 <= self.continue_after_batches <= 100:
            raise ValueError("continue_after_batches must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class SourceDiscoveryResult:
    scan_id: str
    plan_reference: WorkflowArtifactReference
    document_count: int


@dataclass(frozen=True, slots=True)
class SourceBatchInput:
    task_id: str
    tenant_id: str
    connector_name: str
    plan_reference: WorkflowArtifactReference
    start_index: int
    end_index: int
    batch_number: int
    document_concurrency: int


@dataclass(frozen=True, slots=True)
class DocumentIngestionInput:
    task_id: str
    tenant_id: str
    connector_name: str
    plan_reference: WorkflowArtifactReference
    document_index: int


@dataclass(frozen=True, slots=True)
class RawCaptureResult:
    document: DocumentIngestionInput
    document_id: str
    document_version_id: str | None
    decision: str
    connector_type: str | None = None
    content_hash: str | None = None
    source_artifact: WorkflowArtifactReference | None = None
    metadata_artifact: WorkflowArtifactReference | None = None

    def __post_init__(self) -> None:
        if not self.document_id.strip() or not self.decision.strip():
            raise ValueError("raw capture result identity must be non-empty")
        raw_values = (
            self.connector_type,
            self.content_hash,
            self.source_artifact,
            self.metadata_artifact,
        )
        has_raw = all(value is not None for value in raw_values)
        if any(value is not None for value in raw_values) and not has_raw:
            raise ValueError("raw capture reference must be complete")
        if not has_raw and self.document_version_id is None:
            raise ValueError("raw capture without bytes requires an active document version")


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    document: DocumentIngestionInput
    document_id: str
    document_version_id: str
    decision: str
    canonical_reference: WorkflowArtifactReference | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.document_id,
                self.document_version_id,
                self.decision,
            )
        ):
            raise ValueError("prepared document identity must be non-empty")


@dataclass(frozen=True, slots=True)
class DocumentFailureInput:
    document: DocumentIngestionInput
    prepared: PreparedDocument | None
    failed_stage: str
    error_type: str

    def __post_init__(self) -> None:
        if not self.failed_stage.strip() or not self.error_type.strip():
            raise ValueError("document failure stage and type must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceFinalizationInput:
    source: SourceIngestionInput
    scan_id: str
    plan_reference: WorkflowArtifactReference
    summary: DocumentDispatchSummary


@dataclass(frozen=True, slots=True)
class SourceCancellationInput:
    task_id: str

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("cancelled source task ID must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceFailureInput:
    task_id: str
    error_code: str

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.error_code.strip():
            raise ValueError("source failure task ID and error code must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceIngestionResult:
    task_id: str
    scan_id: str
    discovered: int
    published: int
    unchanged: int
    failed: int
    removal_candidates: tuple[str, ...]
    unresolved_relations: int
    status: str = "COMPLETED"

    def __post_init__(self) -> None:
        if self.status not in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
            raise ValueError("source result has an unsupported status")


@dataclass(frozen=True, slots=True)
class SourceIngestionStatus:
    task_id: str
    status: str
    paused: bool
    cancel_requested: bool

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("source status task ID must be non-empty")
        if self.status not in _SOURCE_TASK_STATES:
            raise ValueError("source status is invalid")


@dataclass(frozen=True, slots=True)
class RetryFailuresInput:
    retry_task_id: str
    original_task_id: str
    tenant_id: str
    document_ids: tuple[str, ...]
    document_concurrency: int = 8

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.retry_task_id, self.original_task_id, self.tenant_id)
        ):
            raise ValueError("retry task identities must be non-empty")
        if not self.document_ids or any(not value.strip() for value in self.document_ids):
            raise ValueError("retry task document IDs must be non-empty")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("retry task document IDs must be unique")
        if not 1 <= self.document_concurrency <= 100:
            raise ValueError("retry document_concurrency must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class RetryPreparationResult:
    plan_reference: WorkflowArtifactReference
    document_count: int


@dataclass(frozen=True, slots=True)
class RetryDocumentInput:
    retry_task_id: str
    original_task_id: str
    tenant_id: str
    plan_reference: WorkflowArtifactReference
    document_index: int


@dataclass(frozen=True, slots=True)
class RetryDocumentFailureInput:
    document: RetryDocumentInput
    error_type: str


@dataclass(frozen=True, slots=True)
class RetryFinalizationInput:
    retry_task_id: str
    selected: int
    summary: DocumentDispatchSummary


@dataclass(frozen=True, slots=True)
class RetryFailuresResult:
    retry_task_id: str
    selected: int
    published: int
    unchanged: int
    failed: int
    status: str
