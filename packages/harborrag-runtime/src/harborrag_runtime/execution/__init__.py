"""Direct and durable ingestion execution strategies."""

from __future__ import annotations

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import ExecutionMode
from harborrag_runtime.execution.contracts import IngestionExecutor


def build_ingestion_executor(
    mode: ExecutionMode,
    settings: RuntimeSettings,
) -> IngestionExecutor:
    """Select an execution strategy without leaking its implementation to the SDK."""

    if mode is ExecutionMode.DIRECT:
        from harborrag_runtime.execution.direct import DirectIngestionExecutor

        return DirectIngestionExecutor(settings)

    from harborrag_runtime.execution.temporal import TemporalIngestionExecutor

    return TemporalIngestionExecutor(settings)


__all__ = ["IngestionExecutor", "build_ingestion_executor"]
