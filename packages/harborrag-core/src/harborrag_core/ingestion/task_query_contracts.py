"""Provider-independent task query results exposed by ingestion repositories."""

from __future__ import annotations

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
