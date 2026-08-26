"""Keyset paging over durable ingestion tasks for the public list endpoint.

Split out of service.py (file-length gate) and mixed into
IngestionApplicationService, following the same store-provider pattern as the
rest of that service.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from harborrag_core.invariants import require

from ..errors import IngestionStatusFilterError
from .models import TaskPageCursor
from .ports import TaskStoreProvider
from .presenters import TASK_STATES, decode_task_cursor, encode_task_cursor, task_response


class TaskListingMixin:
    """Read one page of task summaries in the get-by-id response shape."""

    _task_store_provider: TaskStoreProvider

    async def list_tasks(
        self,
        *,
        tenants: frozenset[str] | None,
        statuses: Sequence[str] | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """List tasks newest submission first, restricted to ``tenants``.

        ``tenants`` is the caller's authorized scope resolved by the
        transport; None means unrestricted, so a wildcard principal sees
        every tenant and a scoped one can never page past its own. Progress
        counts for the whole page come from a single grouped query rather
        than one ``progress`` call per row.
        """

        store = await self._task_store_provider()
        position = decode_task_cursor(cursor) if cursor else None
        page = await store.list_tasks(
            tenant_ids=tenants,
            statuses=_resolve_statuses(statuses),
            before_submitted_at=(
                datetime.fromisoformat(position.submitted_at) if position is not None else None
            ),
            before_task_id=(position.task_id if position is not None else None),
            limit=limit,
        )
        counts = await store.progress_for_tasks([task.task_id for task in page.items])
        items = [task_response(task, counts.get(task.task_id, {})) for task in page.items]
        next_cursor = None
        if page.has_more and page.items:
            last = page.items[-1]
            last_submitted_at = require(
                last.submitted_at,
                "paged ingestion task is missing submitted_at",
            )
            next_cursor = encode_task_cursor(
                TaskPageCursor(
                    submitted_at=last_submitted_at.isoformat(),
                    task_id=last.task_id,
                )
            )
        return {"items": items, "next_cursor": next_cursor}


def _resolve_statuses(statuses: Sequence[str] | None) -> tuple[str, ...] | None:
    """Translate public status names into the durable states they select.

    A route validates its status filter against the public enum before this
    runs, so an unknown name here can only reach a caller that skipped that
    check -- raise the same validation error rather than a raw ``KeyError``.
    """

    if statuses is None:
        return None
    resolved: list[str] = []
    for name in statuses:
        matched = TASK_STATES.get(name)
        if matched is None:
            raise IngestionStatusFilterError(f"Unknown ingestion status filter: {name!r}")
        resolved.extend(matched)
    return tuple(resolved)


__all__ = ["TaskListingMixin"]
