"""Start an ingestion of a connection and read the queued run back.

Handles:
- The project check, then the connection's own checks and the start
- A project or connection that does not exist as a reference error naming the field
- The queued run as the spec's ``Ingestion`` object, with its links

Does NOT choose the response's status or ``Location``; the transport does.
"""

from __future__ import annotations

from typing import Any

from harborrag_core.contracts.errors import HarborNotFoundError, UnknownReferenceError
from harborrag_engine.ingestion.operations import IngestionOperations

from ..control_plane.connections import ConnectionKey, ConnectionService
from ..control_plane.projects import ProjectService


class IngestionStarter:
    """Starts ingestions through the connection service and reads them back with links.

    Args:
        projects: Project reads, for the existence check.
        connections: Connection use cases that start the run.
        runs: Run reads that give the queued run its links.
    """

    def __init__(
        self, *, projects: ProjectService, connections: ConnectionService, runs: IngestionOperations
    ) -> None:
        self._projects = projects
        self._connections = connections
        self._runs = runs

    async def start(  # noqa: PLR0913 - the connection, who triggered it and the run's options
        self,
        key: ConnectionKey,
        *,
        actor: str,
        actor_type: str,
        mode: str,
        item_refs: tuple[str, ...],
        updated_after: str | None,
    ) -> dict[str, Any]:
        """Start an incremental or forced ingestion of a connection.

        Args:
            key: The connection to ingest.
            actor: Subject recorded as who triggered the run.
            actor_type: ``api_key`` or ``user``.
            mode: ``incremental`` or ``forced``.
            item_refs: Items to ingest alone; refused for sources that cannot.
            updated_after: ISO timestamp that narrows the run to newer items.

        Returns:
            The queued run as the spec's ``Ingestion`` object.

        Raises:
            UnknownReferenceError: The project or the connection does not exist; the
                ``field`` detail names which.
            HarborAlreadyRunningError: The connection already has a run in progress.
            HarborCapabilityError: ``item_refs`` on a source that cannot ingest items.
        """
        try:
            await self._projects.get(tenant_id=key.tenant_id, project_id=key.project_id)
        except HarborNotFoundError as exc:
            raise UnknownReferenceError(str(exc), {"field": "project_id"}) from exc
        try:
            started = await self._connections.sync(
                key,
                actor=actor,
                actor_type=actor_type,
                mode=mode,
                item_refs=item_refs,
                updated_after=updated_after,
            )
        except HarborNotFoundError as exc:
            raise UnknownReferenceError(str(exc), {"field": "connection_id"}) from exc
        return await self._runs.get(tenant_id=key.tenant_id, ingestion_id=started["ingestion_id"])
