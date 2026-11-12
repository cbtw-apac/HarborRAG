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
    run_id: str
    workflow_id: str
    first_execution_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionExecutionStatus:
    task_id: str
    status: str
    paused: bool
    cancel_requested: bool
    pause_applied: bool = False


@dataclass(frozen=True, slots=True)
class IngestionExecutionResult:
    task_id: str
    scan_id: str
    discovered: int
    published: int
    unchanged: int
    failed: int
    # A bounded sample of the removed documents; ``removal_count`` has them all.
    removal_candidates: tuple[str, ...]
    unresolved_relations: int
    status: str = "COMPLETED"
    removal_count: int | None = None

    def __post_init__(self) -> None:
        if self.removal_count is None:
            object.__setattr__(self, "removal_count", len(self.removal_candidates))


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

    async def execution_status(self, task_id: str, kind: RunKind = RunKind.SOURCE) -> str: ...

    async def result(self, task_id: str) -> IngestionExecutionResult: ...

    async def pause(self, task_id: str) -> None: ...

    async def resume(self, task_id: str) -> None: ...

    async def cancel(self, task_id: str, kind: RunKind = RunKind.SOURCE) -> None: ...

    async def health(self) -> bool: ...
