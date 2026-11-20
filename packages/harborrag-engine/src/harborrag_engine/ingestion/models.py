"""Transport-neutral ingestion commands and cursors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IngestionCreateCommand:
    """A request to start an ingestion of one connection."""

    tenant_id: str
    connection_id: str
    force_reprocess: bool
    public_request: dict[str, object]
    source_scope_id: str | None = None
    updated_after: str | None = None
    actor: str | None = None
    actor_type: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentPageCursor:
    """Position after the last document of a page."""

    updated_at: str
    document_id: str


@dataclass(frozen=True, slots=True)
class TaskPageCursor:
    """Position after the last task of a page."""

    submitted_at: str
    task_id: str
