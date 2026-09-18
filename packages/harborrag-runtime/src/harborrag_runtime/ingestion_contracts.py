"""Execution-provider-neutral ingestion submissions, outcomes, and gateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harborrag_runtime.ingestion.limits import (
    MAX_RETRY_DOCUMENT_IDS,
    validate_discovery_concurrency,
    validate_discovery_page_size,
    validate_document_concurrency,
    validate_source_orchestration_limits,
)
from harborrag_runtime.source_query import ProcessingProfileInput, SourceQuery


@dataclass(frozen=True, slots=True)
class SourceSubmission:
    """Secret-free request resolved against the configured connector catalog."""

    task_id: str
    tenant_id: str
    connector_name: str
    connection_id: str | None = None
    source_scope_id: str | None = None
    query: SourceQuery = SourceQuery()
    force_reprocess: bool = False
    discovery_page_size: int = 50
    discovery_concurrency: int = 4
    document_concurrency: int | None = None
    missing_threshold: int = 2
    batch_size: int | None = None
    continue_after_batches: int = 25


@dataclass(frozen=True, slots=True)
class PreparedSourceSubmission:
    """Resolved ingestion input without workflow history or provider options."""

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

    def __post_init__(self) -> None:
        identities = (
            self.task_id,
            self.tenant_id,
            self.connector_name,
            self.connector_type,
            self.connection_id,
            self.source_scope_id,
            self.configuration_fingerprint,
        )
        if any(not value.strip() for value in identities):
            raise ValueError("source input identities must be non-empty")
        if len(self.task_id) > 128:
            raise ValueError("source task ID must not exceed 128 characters")
        validate_source_orchestration_limits(
            batch_size=self.batch_size, continue_after_batches=self.continue_after_batches
        )
        validate_document_concurrency(self.document_concurrency)
        validate_discovery_page_size(self.discovery_page_size)
        validate_discovery_concurrency(self.discovery_concurrency)
        if self.missing_threshold < 1:
            raise ValueError("missing_threshold must be positive")


@dataclass(frozen=True, slots=True)
class IngestionRetryRequest:
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
        if len(self.document_ids) > MAX_RETRY_DOCUMENT_IDS:
            raise ValueError(
                f"retry task document IDs must number at most {MAX_RETRY_DOCUMENT_IDS}"
            )
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("retry task document IDs must be unique")
        if not 1 <= self.document_concurrency <= 100:
            raise ValueError("retry document_concurrency must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class IngestionExecutionReference:
    run_id: str
    workflow_id: str
    first_execution_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionExecutionStatus:
    task_id: str
    status: str
    paused: bool
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class IngestionExecutionResult:
    task_id: str
    scan_id: str
    discovered: int
    published: int
    unchanged: int
    failed: int
    removal_candidates: tuple[str, ...]
    unresolved_relations: int
    status: str = "COMPLETED"


class IngestionGateway(Protocol):
    """Durable ingestion capability consumed by application services."""

    async def start_ingestion(
        self, source: PreparedSourceSubmission
    ) -> IngestionExecutionReference: ...

    async def start_retry_failures(
        self, request: IngestionRetryRequest
    ) -> IngestionExecutionReference: ...

    async def get_status(self, task_id: str) -> IngestionExecutionStatus: ...

    async def get_progress(self, task_id: str) -> dict[str, int]: ...

    async def execution_status(self, task_id: str) -> str: ...

    async def result(self, task_id: str) -> IngestionExecutionResult: ...

    async def pause(self, task_id: str) -> None: ...

    async def resume(self, task_id: str) -> None: ...

    async def cancel(self, task_id: str) -> None: ...

    async def health(self) -> bool: ...
