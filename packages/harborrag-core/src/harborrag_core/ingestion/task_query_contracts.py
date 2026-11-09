"""Provider-independent task query results exposed by ingestion repositories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .lifecycle_contracts import IngestionTask


@dataclass(frozen=True, slots=True)
class TaskRegistration:
    """Outcome of idempotently registering an ingestion task."""

    task: IngestionTask
    created: bool


@dataclass(frozen=True, slots=True)
class StoredTaskDocumentResult:
    """Bounded public view of one persisted document outcome."""

    task_id: str
    document_id: str
    document_version_id: str | None
    status: str
    result: dict[str, object]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskDocumentPage:
    """Cursor page of persisted document outcomes."""

    items: tuple[StoredTaskDocumentResult, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class StoredFailure:
    """Latest failed result of one document, with its retry history."""

    task_id: str
    document_id: str
    source_key: str
    result: Mapping[str, object]
    failed_at: datetime
    attempts: int
    resolved: bool


@dataclass(frozen=True, slots=True)
class FailurePage:
    """Keyset page of failure inbox items.

    ``counts`` covers every item matching the filter, not only this page;
    ``total`` is set only when the caller asked for it.
    """

    items: tuple[StoredFailure, ...]
    has_more: bool
    counts: dict[str, int]
    total: int | None


@dataclass(frozen=True, slots=True)
class TaskPage:
    """Cursor page of durable ingestion tasks, newest submission first."""

    items: tuple[IngestionTask, ...]
    has_more: bool
