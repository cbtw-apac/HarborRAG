"""Read ingestion runs as the spec's ``Ingestion`` object, scoped to one tenant."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, Protocol

from harborrag_core.domain.connection import Connection
from harborrag_core.ingestion import IngestionTask, IngestionTaskState, TaskPage
from harborrag_core.invariants import require
from harborrag_core.ports.control_plane import ConnectionRepositoryPort

from ..control_plane.connection_views import INGESTION_STATES, IngestionLinks, ingestion_view
from ..errors import IngestionActionStateError, IngestionCursorError, IngestionNotFoundError
from .models import TaskPageCursor
from .presenters import TERMINAL_STATES, decode_task_cursor, encode_task_cursor

ControlAction = Literal["pause", "resume", "cancel"]
ACTION_WAIT_SECONDS: dict[str, int] = {"pause": 10, "resume": 10, "cancel": 30}
ACTION_POLL_SECONDS = 0.5
_ALLOWED_FROM: dict[str, frozenset[str]] = {
    "pause": frozenset({"queued", "running"}),
    "resume": frozenset({"paused"}),
    "cancel": frozenset({"queued", "running", "paused"}),
    "retry-failures": frozenset({"succeeded", "failed", "cancelled"}),
}
_TARGET_STATES: dict[str, frozenset[IngestionTaskState]] = {
    "pause": frozenset({IngestionTaskState.PAUSED}),
    "resume": frozenset({IngestionTaskState.RUNNING}),
    "cancel": frozenset(TERMINAL_STATES),
}


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


class IngestionActions(Protocol):
    """The workflow actions on a run."""

    async def pause(self, task_id: str, *, actor: str | None = None) -> dict[str, object]: ...

    async def resume(self, task_id: str, *, actor: str | None = None) -> dict[str, object]: ...

    async def cancel(self, task_id: str, *, actor: str | None = None) -> dict[str, object]: ...

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: Sequence[str],
        actor: str | None = None,
        actor_type: str | None = None,
    ) -> dict[str, object]: ...


class IngestionOperations:
    """List, read and control ingestion runs of one tenant."""

    def __init__(  # noqa: PLR0913 - the run reads plus the action seam
        self,
        *,
        connections: Callable[[], ConnectionRepositoryPort],
        runs: Callable[[], Awaitable[IngestionRuns]],
        links: IngestionLinks,
        actions: IngestionActions,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._connections = connections
        self._runs = runs
        self._links = links
        self._actions = actions
        self._sleep = sleep

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

    async def act(
        self, *, tenant_id: str, ingestion_id: str, action: ControlAction, actor: str
    ) -> dict[str, Any]:
        """Pause, resume or cancel a run and answer with it once the action took effect.

        Waits for the target status up to ``ACTION_WAIT_SECONDS[action]``; on timeout the
        run is answered as it stands.

        Args:
            tenant_id: Tenant owning the run.
            ingestion_id: The run.
            action: What to do.
            actor: Principal id recorded on the run's event trail.

        Returns:
            The run's ``Ingestion`` object.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
            IngestionActionStateError: The run's status does not allow ``action``.
        """
        _require_allowed(action, await self.get(tenant_id=tenant_id, ingestion_id=ingestion_id))
        await getattr(self._actions, action)(ingestion_id, actor=actor)
        await self._wait_for(ingestion_id, _TARGET_STATES[action], ACTION_WAIT_SECONDS[action])
        return await self.get(tenant_id=tenant_id, ingestion_id=ingestion_id)

    async def retry_failures(
        self,
        *,
        tenant_id: str,
        ingestion_id: str,
        actor: str,
        actor_type: str,
        document_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Start a retry run of a finished run's failed documents.

        Args:
            tenant_id: Tenant owning the run.
            ingestion_id: The finished run.
            actor: Principal id of the caller.
            actor_type: ``user`` or ``api_key``.
            document_ids: Documents to retry; empty retries every retryable failure.

        Returns:
            The new run's ``Ingestion`` object.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
            IngestionActionStateError: The run is active or has no failed documents.
        """
        current = await self.get(tenant_id=tenant_id, ingestion_id=ingestion_id)
        _require_allowed("retry-failures", current)
        if not current["counters"]["failed"]:
            raise IngestionActionStateError("the ingestion has no failed documents to retry")
        retry = await self._actions.retry_failures(
            task_id=ingestion_id, document_ids=document_ids, actor=actor, actor_type=actor_type
        )
        return await self.get(tenant_id=tenant_id, ingestion_id=str(retry["retry_task_id"]))

    async def _wait_for(
        self, ingestion_id: str, states: frozenset[IngestionTaskState], seconds: float
    ) -> None:
        """Poll the run until it is in one of ``states`` or ``seconds`` have passed."""
        runs = await self._runs()
        waited = 0.0
        while waited < seconds:
            task = await runs.get(ingestion_id)
            if task is None or task.status in states:
                return
            await self._sleep(ACTION_POLL_SECONDS)
            waited += ACTION_POLL_SECONDS


def _require_allowed(action: str, ingestion: Mapping[str, Any]) -> None:
    if ingestion["status"] not in _ALLOWED_FROM[action]:
        raise IngestionActionStateError(
            f"cannot {action} an ingestion that is {ingestion['status']}"
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
