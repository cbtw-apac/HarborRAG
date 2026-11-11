"""Find a connection's active ingestion run after settling runs whose workflow ended unrecorded.

Settling the newest active run can reveal an older one, such as a sync behind a terminated
retry run, so the lookup repeats. A run still active after ``SETTLE_ROUNDS`` settles counts as
active, so the guards that use it fail closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_core.ingestion import IngestionTask

if TYPE_CHECKING:
    from .connections import ConnectionRuns, IngestionSubmitter

SETTLE_ROUNDS = 5


async def active_run(
    runs: ConnectionRuns, ingestions: IngestionSubmitter, *, tenant_id: str, source_key: str
) -> IngestionTask | None:
    """Return the connection's pending, running or paused run, settling ended runs first.

    Args:
        runs: Run registry of the connection.
        ingestions: Settles a run whose workflow ended without recording it.
        tenant_id: Tenant of the connection.
        source_key: The connection's source key.

    Returns:
        The first active run that does not settle, or ``None`` once no active run is left.
    """
    for _ in range(SETTLE_ROUNDS):
        active = await runs.active_for_connection(tenant_id=tenant_id, connection_id=source_key)
        if active is None or not await ingestions.settle_if_ended(active.task_id):
            return active
    return await runs.active_for_connection(tenant_id=tenant_id, connection_id=source_key)
