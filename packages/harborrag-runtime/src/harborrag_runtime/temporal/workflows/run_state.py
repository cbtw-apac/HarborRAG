"""Pure updates for signal-mutated ingestion run state."""

from __future__ import annotations

from harborrag_runtime.temporal.schemas import PendingResolution


def record_pending(
    pending_items: list[PendingResolution],
    pending: PendingResolution,
) -> list[PendingResolution]:
    retained = [item for item in pending_items if item.artifact_id != pending.artifact_id]
    retained.append(pending)
    return retained


def cancellation_received(value: bool) -> bool:
    """Read signal-mutated state without invalid narrowing across awaits."""

    return value
