"""Read ingestion runs as the spec's ``Ingestion`` object, scoped to one tenant."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol

from harborrag_core.domain.connection import Connection
from harborrag_core.ingestion import IngestionTask, TaskPage
from harborrag_core.invariants import require
from harborrag_core.ports.control_plane import ConnectionRepositoryPort

from ..control_plane.connection_views import INGESTION_STATES, IngestionLinks, ingestion_view
from ..errors import IngestionCursorError, IngestionNotFoundError
from .models import TaskPageCursor
from .presenters import decode_task_cursor, encode_task_cursor


class IngestionRuns(Protocol):
    """The durable run registry the reads need."""

    async def get(self, task_id: str) -> IngestionTask | None: ...

    async def progress(self, task_id: str) -> dict[str, int]: ...

    async def progress_for_tasks(self, task_ids: Sequence[str]) -> dict[str, dict[str, int]]: ...

    async def list_tasks(  # noqa: PLR0913 - the registry's listing filters
        self,
        *,
        tenant_ids: frozenset[str] | None = None,
        statuses: Sequence[str] | None = None,
        before_submitted_at: datetime | None = None,
        before_task_id: str | None = None,
        scope_connection_ids: frozenset[str] | None = None,
        created_after: datetime | None = None,
        limit: int = 50,
    ) -> TaskPage: ...


async def connection_index(
    connections: ConnectionRepositoryPort, *, tenant_id: str, project_id: str | None = None
) -> dict[str, Connection]:
    """Return the tenant's connections keyed by ``source_key``.

    Args:
        connections: Connection repository.
        tenant_id: Tenant to read.
        project_id: Keep one project only.

    Returns:
        The connections the runs' stored ``connection_id`` refers to.
    """
    found, _ = await connections.list(tenant_ids=frozenset({tenant_id}), project_id=project_id)
    return {connection.source_key: connection for connection in found}


async def source_keys_for(
    connections: ConnectionRepositoryPort,
    *,
    tenant_id: str,
    project_id: str | None,
    connection_ref: str | None,
) -> frozenset[str] | None:
    """Return the source keys a project and connection filter selects.

    Args:
        connections: Connection repository.
        tenant_id: Tenant to read.
        project_id: Keep one project only.
        connection_ref: Keep one ``project_id/connection_id`` only.

    Returns:
        ``None`` when neither filter is set; otherwise the matching keys, empty when
        nothing matches.
    """
    index = await connection_index(connections, tenant_id=tenant_id, project_id=project_id)
    return _keys(index, project_id=project_id, connection_ref=connection_ref)


def _keys(
    index: Mapping[str, Connection], *, project_id: str | None, connection_ref: str | None
) -> frozenset[str] | None:
    if project_id is None and connection_ref is None:
        return None
    return frozenset(
        key
        for key, connection in index.items()
        if (project_id is None or connection.project_id == project_id)
        and (connection_ref is None or connection.ref == connection_ref)
    )


class IngestionOperations:
    """List and read ingestion runs of one tenant."""

    def __init__(
        self,
        *,
        connections: Callable[[], ConnectionRepositoryPort],
        runs: Callable[[], Awaitable[IngestionRuns]],
        links: IngestionLinks,
    ) -> None:
        self._connections = connections
        self._runs = runs
        self._links = links

    async def list(  # noqa: PLR0913 - the spec's list filters plus paging
        self,
        *,
        tenant_id: str,
        project_id: str | None,
        connection_ref: str | None,
        status: str | None,
        created_after: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """Return a newest-first page of the tenant's runs.

        Args:
            tenant_id: Tenant to read.
            project_id: Keep the runs of one project.
            connection_ref: Keep the runs of one connection.
            status: Keep one spec status.
            created_after: Keep runs created at or after this time.
            cursor: ``next_cursor`` of the previous page, for the same filters.
            limit: Page size.

        Returns:
            ``{"items": [Ingestion], "next_cursor": str | None}``.

        Raises:
            IngestionCursorError: ``cursor`` is malformed or belongs to other filters.
        """
        fingerprint = _fingerprint(project_id, connection_ref, status, created_after)
        position = _decode_cursor(cursor, fingerprint) if cursor else None
        index = await connection_index(self._connections(), tenant_id=tenant_id)
        runs = await self._runs()
        page = await runs.list_tasks(
            tenant_ids=frozenset({tenant_id}),
            statuses=INGESTION_STATES[status] if status is not None else None,
            before_submitted_at=(
                datetime.fromisoformat(position.submitted_at) if position else None
            ),
            before_task_id=position.task_id if position else None,
            scope_connection_ids=_keys(index, project_id=project_id, connection_ref=connection_ref),
            created_after=created_after,
            limit=limit,
        )
        counts = await runs.progress_for_tasks([task.task_id for task in page.items])
        items = [
            ingestion_view(
                task,
                connection=index.get(str(task.request.get("connection_id"))),
                progress=counts.get(task.task_id),
                links=self._links,
            )
            for task in page.items
        ]
        next_cursor = None
        if page.has_more and page.items:
            last = page.items[-1]
            submitted_at = require(last.submitted_at, "paged ingestion task has no submitted_at")
            next_cursor = _encode_cursor(
                TaskPageCursor(submitted_at=submitted_at.isoformat(), task_id=last.task_id),
                fingerprint,
            )
        return {"items": items, "next_cursor": next_cursor}

    async def get(self, *, tenant_id: str, ingestion_id: str) -> dict[str, Any]:
        """Return one run of the tenant.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
        """
        runs = await self._runs()
        task = await runs.get(ingestion_id)
        if task is None or task.request.get("tenant_id") != tenant_id:
            raise IngestionNotFoundError(f"ingestion {ingestion_id!r} not found")
        connection = await self._connections().get_by_source_key(
            tenant_id=tenant_id, source_key=str(task.request.get("connection_id"))
        )
        return ingestion_view(
            task,
            connection=connection,
            progress=await runs.progress(ingestion_id),
            links=self._links,
        )


def _fingerprint(
    project_id: str | None,
    connection_ref: str | None,
    status: str | None,
    created_after: datetime | None,
) -> str:
    parts = (
        project_id,
        connection_ref,
        status,
        created_after.isoformat() if created_after else None,
    )
    return sha256("|".join(part or "" for part in parts).encode()).hexdigest()[:16]


def _encode_cursor(position: TaskPageCursor, fingerprint: str) -> str:
    return f"{fingerprint}.{encode_task_cursor(position)}"


def _decode_cursor(cursor: str, fingerprint: str) -> TaskPageCursor:
    bound, _, position = cursor.partition(".")
    if bound != fingerprint:
        raise IngestionCursorError("Cursor does not match the filters.", {"field": "cursor"})
    return decode_task_cursor(position)
