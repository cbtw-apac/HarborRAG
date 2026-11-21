"""Value types of the connection service: who writes, what is written and what it returns.

Leaf module of the connection use cases; it imports none of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from harborrag_core.contracts.errors import HarborError
from harborrag_core.domain.connection import Connection
from harborrag_core.ingestion import IngestionTask
from harborrag_core.security import AccessContext
from harborrag_engine.ingestion.models import IngestionCreateCommand

WriteOrigin = Literal["api", "operator"]
"""Who writes: ``api`` callers may not set new ``env:`` references or operator-only
fields such as a worker file path; the CLI (``operator``) may."""


class ConnectionRuns(Protocol):
    """Ingestion runs and published-document counts of one connection."""

    async def latest_for_connection(
        self, *, tenant_id: str, connection_id: str
    ) -> IngestionTask | None:
        """Return the connection's most recently submitted run, or ``None`` when it has none."""

    async def active_for_connection(
        self, *, tenant_id: str, connection_id: str
    ) -> IngestionTask | None:
        """Return the connection's run that has not ended, or ``None`` when it has none."""

    async def connection_document_count(self, *, tenant_id: str, connection_id: str) -> int:
        """Count the documents the connection's runs have recorded."""

    async def get(self, task_id: str) -> IngestionTask | None:
        """Return the run ``task_id``, or ``None`` when it does not exist."""

    async def progress(self, task_id: str) -> dict[str, int]:
        """Return the run's document counts by result status."""

    async def progress_for_tasks(self, task_ids: Sequence[str]) -> dict[str, dict[str, int]]:
        """Return the document counts by result status for each of ``task_ids``."""

    async def source_scope_ids_by_connection(
        self, *, tenant_id: str, connection_ids: Sequence[str]
    ) -> dict[str, list[str]]:
        """Return the source scopes each connection, by ``source_key``, was ingested under."""

    async def allowed_source_scope_ids(
        self, tenant_id: str, *, access: AccessContext, limit: int = 10000
    ) -> tuple[str, ...]:
        """Return every source scope ``access`` may read in ``tenant_id``.

        Raises:
            HarborConflictError: The reader may read more than ``limit`` source scopes.
        """


class IngestionSubmitter(Protocol):
    """Start an ingestion through the shared submission path; tell when a run ended unrecorded."""

    async def submit(
        self, command: IngestionCreateCommand, *, idempotency_key: str | None
    ) -> dict[str, object]:
        """Register an ingestion and start its workflow; the accepted task as a public document."""

    async def settle_if_ended(self, task_id: str) -> bool:
        """Record the end of a run whose workflow ended unrecorded; whether the run is over."""


@dataclass(frozen=True, slots=True)
class ConnectionWrite:
    """The body of a connection ``PUT`` or ``test``."""

    source_type: str
    credentials: Mapping[str, Any]
    config: Mapping[str, Any]
    name: str | None = None
    tuning: Mapping[str, Any] | None = None

    def effective_tuning(self, existing: Connection | None) -> Mapping[str, Any]:
        """Return the tuning to apply: as sent, else what is stored, else none."""
        if self.tuning is not None:
            return self.tuning
        return existing.tuning if existing else {}


@dataclass(frozen=True, slots=True)
class ConnectionPutResult:
    """A stored connection, whether it was new, and what ``sync_now`` started or why it did not."""

    connection: dict[str, Any]
    created: bool
    version: int
    ingestion: dict[str, Any] | None = None
    sync_error: HarborError | None = None


@dataclass(frozen=True, slots=True)
class ConnectionKey:
    """The public identity of a connection inside one tenant."""

    tenant_id: str
    project_id: str
    connection_id: str
