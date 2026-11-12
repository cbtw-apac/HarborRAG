"""Execution-provider-neutral ingestion outcomes and gateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harborrag_core.ingestion.run_contracts import (
    IngestionRetryRequest,
    PreparedSourceSubmission,
    RunKind,
)


@dataclass(frozen=True, slots=True)
class IngestionExecutionReference:
    """Handle of a started ingestion execution."""

    run_id: str
    workflow_id: str
    first_execution_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionExecutionStatus:
    """Control-plane view of one execution's state."""

    task_id: str
    status: str
    paused: bool
    cancel_requested: bool
    pause_applied: bool = False


@dataclass(frozen=True, slots=True)
class IngestionExecutionResult:
    """Counts a finished source execution reports.

    Args:
        task_id: Run the counts belong to.
        scan_id: Discovery scan of the run.
        discovered: Documents discovered.
        published: Documents published.
        unchanged: Documents left as they were.
        failed: Documents that failed.
        removal_candidates: A bounded sample of the removed documents.
        unresolved_relations: Relations left unresolved.
        status: Final task state name.
        removal_count: Every removed document; defaults to the sample size.
    """

    task_id: str
    scan_id: str
    discovered: int
    published: int
    unchanged: int
    failed: int
    removal_candidates: tuple[str, ...]
    unresolved_relations: int
    status: str = "COMPLETED"
    removal_count: int | None = None

    def __post_init__(self) -> None:
        """Default ``removal_count`` to the sample size."""
        if self.removal_count is None:
            object.__setattr__(self, "removal_count", len(self.removal_candidates))


class IngestionGateway(Protocol):
    """Durable ingestion capability consumed by application services."""

    async def start_ingestion(
        self, source: PreparedSourceSubmission
    ) -> IngestionExecutionReference:
        """Start the source ingestion run for ``source``."""

    async def start_retry_failures(
        self, request: IngestionRetryRequest
    ) -> IngestionExecutionReference:
        """Start a retry run for the failed documents named in ``request``."""

    async def get_status(self, task_id: str) -> IngestionExecutionStatus:
        """Return the execution status of run ``task_id``."""

    async def get_progress(self, task_id: str) -> dict[str, int]:
        """Return the document counts of run ``task_id``."""

    async def execution_status(self, task_id: str, kind: RunKind = RunKind.SOURCE) -> str:
        """Return the provider's status string for run ``task_id`` of ``kind``."""

    async def result(self, task_id: str) -> IngestionExecutionResult:
        """Return the final result of run ``task_id``."""

    async def pause(self, task_id: str) -> None:
        """Pause run ``task_id``."""

    async def resume(self, task_id: str) -> None:
        """Resume run ``task_id``."""

    async def cancel(self, task_id: str, kind: RunKind = RunKind.SOURCE) -> None:
        """Cancel run ``task_id`` of ``kind``."""

    async def health(self) -> bool:
        """Return whether the execution provider is reachable."""
