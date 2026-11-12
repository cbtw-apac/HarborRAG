"""Run kinds, pinned run settings and the submissions that start an ingestion run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from harborrag_core.ingestion.limits import (
    MAX_RETRY_DOCUMENT_IDS,
    validate_discovery_concurrency,
    validate_discovery_page_size,
    validate_document_concurrency,
    validate_source_orchestration_limits,
)
from harborrag_core.ingestion.source_query import ProcessingProfileInput, SourceQuery


class RunKind(StrEnum):
    """The workflow an ingestion run executes as, named by its workflow id prefix."""

    SOURCE = "harborrag-source"
    RETRY = "harborrag-retry"

    @classmethod
    def of(cls, request: Mapping[str, object]) -> RunKind:
        """Return the kind of the run whose stored request is ``request``."""
        return cls.RETRY if request.get("retry_of") else cls.SOURCE

    def workflow_id(self, task_id: str) -> str:
        """Return the workflow id of run ``task_id``."""
        return f"{self.value}:{task_id}"


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
class RunSettings:
    """Overridden ``next_ingestion`` settings pinned for one run.

    ``None`` leaves the boot knob in charge.

    Args:
        settings_revision: Revision label the values were resolved at.
        pdf_parser: PDF backend override.
        chunk_max_tokens: Cap on every chunking profile's hard maximum.
        document_concurrency: Concurrent documents override.
    """

    settings_revision: str
    pdf_parser: str | None = None
    chunk_max_tokens: int | None = None
    document_concurrency: int | None = None

    def to_json(self) -> dict[str, object]:
        """Return the JSON-safe form stored in task requests and plans."""
        return {
            "settings_revision": self.settings_revision,
            "pdf_parser": self.pdf_parser,
            "chunk_max_tokens": self.chunk_max_tokens,
            "document_concurrency": self.document_concurrency,
        }

    @classmethod
    def from_json(cls, value: object) -> RunSettings | None:
        """Rebuild from :meth:`to_json`; ``None`` for anything that is not a stored run."""
        if not isinstance(value, dict):
            return None
        revision = value.get("settings_revision")
        pdf_parser = value.get("pdf_parser")
        max_tokens = value.get("chunk_max_tokens")
        concurrency = value.get("document_concurrency")
        if (
            not isinstance(revision, str)
            or not (pdf_parser is None or isinstance(pdf_parser, str))
            or not (max_tokens is None or isinstance(max_tokens, int))
            or not (concurrency is None or isinstance(concurrency, int))
        ):
            return None
        return cls(revision, pdf_parser, max_tokens, concurrency)


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
    run_settings: RunSettings | None = None

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
