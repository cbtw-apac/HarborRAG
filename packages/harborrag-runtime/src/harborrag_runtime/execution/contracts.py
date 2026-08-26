"""Execution strategy contracts used by the SDK orchestration layer."""

from __future__ import annotations

from typing import Protocol

from harborrag_runtime.contracts import (
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
    IngestionTaskReference,
)


class IngestionExecutor(Protocol):
    async def start(self) -> None: ...

    async def run(self, request: IngestionRequest) -> IngestionResult: ...

    async def aclose(self) -> None: ...


class DurableIngestionExecutor(IngestionExecutor, Protocol):
    async def submit(self, request: IngestionRequest) -> IngestionTaskReference: ...

    async def status(self, task_id: str) -> IngestionStatus: ...

    async def pause(self, task_id: str) -> None: ...

    async def resume(self, task_id: str) -> None: ...

    async def cancel(self, task_id: str) -> None: ...
