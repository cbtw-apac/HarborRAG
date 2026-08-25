from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, insert, or_, select
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import (
    IngestionTask,
    IngestionTaskState,
    TaskPage,
    TaskRegistration,
    reject_runtime_fields,
)
from harborrag_core.invariants import HarborInvariantError

from .row_values import DatabaseRow, optional_datetime, required_mapping, required_text
from .schema import INGESTION_TASKS
from .task_lifecycle import TaskLifecycleMixin
from .task_results import (
    StoredTaskDocumentResult,
    TaskDocumentPage,
    TaskDocumentResultsMixin,
)

__all__ = [
    "IngestionTaskRepository",
    "StoredTaskDocumentResult",
    "TaskDocumentPage",
    "TaskPage",
    "TaskRegistration",
]


class IngestionTaskRepository(TaskLifecycleMixin, TaskDocumentResultsMixin):
    """Persist API task lifecycle and bounded per-document outcomes."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def create(self, task: IngestionTask) -> None:
        await self.register(task)

    async def register(
        self,
        task: IngestionTask,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> TaskRegistration:
        """Create a task or return the task reserved by the same public request."""

        if task.status != IngestionTaskState.PENDING:
            raise ValueError("new ingestion tasks must begin in PENDING")
        reject_runtime_fields(task.request)
        tenant_id = str(task.request.get("tenant_id") or "DEFAULT")
        self._validate_idempotency(idempotency_key, request_hash)
        now = utc_now()
        try:
            async with self._client.sessions.begin() as session:
                if idempotency_key is not None:
                    existing_key = await session.execute(
                        select(INGESTION_TASKS)
                        .where(
                            INGESTION_TASKS.c.tenant_id == tenant_id,
                            INGESTION_TASKS.c.idempotency_key == idempotency_key,
                        )
                        .with_for_update()
                    )
                    key_row = existing_key.mappings().one_or_none()
                    if key_row is not None:
                        return self._match_idempotent_task(
                            key_row,
                            request_hash=request_hash,
                        )
                result = await session.execute(
                    select(INGESTION_TASKS).where(INGESTION_TASKS.c.task_id == task.task_id)
                )
                row = result.mappings().one_or_none()
                if row is None:
                    await session.execute(
                        insert(INGESTION_TASKS).values(
                            task_id=task.task_id,
                            tenant_id=tenant_id,
                            source_scope_id=task.source_scope_id,
                            status=task.status.value,
                            submitted_at=now,
                            request=task.request,
                            summary=task.summary,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                        )
                    )
                    return TaskRegistration(
                        task.model_copy(update={"submitted_at": now}),
                        created=True,
                    )
                if (
                    row["source_scope_id"],
                    row["request"],
                ) != (task.source_scope_id, task.request):
                    raise HarborConflictError("ingestion task identity collision")
                return TaskRegistration(_task_from_row(row), created=False)
        except IntegrityError as error:
            if idempotency_key is not None:
                async with self._client.sessions() as session:
                    result = await session.execute(
                        select(INGESTION_TASKS).where(
                            INGESTION_TASKS.c.tenant_id == tenant_id,
                            INGESTION_TASKS.c.idempotency_key == idempotency_key,
                        )
                    )
                    row = result.mappings().one_or_none()
                if row is not None:
                    return self._match_idempotent_task(
                        row,
                        request_hash=request_hash,
                    )
            raise HarborConflictError("ingestion task identity collision") from error

    @staticmethod
    def _match_idempotent_task(
        row: DatabaseRow,
        *,
        request_hash: str | None,
    ) -> TaskRegistration:
        if row["request_hash"] != request_hash:
            raise HarborConflictError(
                "idempotency key was already used for a different ingestion request"
            )
        return TaskRegistration(_task_from_row(row), created=False)

    @staticmethod
    def _validate_idempotency(
        idempotency_key: str | None,
        request_hash: str | None,
    ) -> None:
        if (idempotency_key is None) != (request_hash is None):
            raise ValueError("idempotency key and request hash must be supplied together")
        if idempotency_key is None:
            return
        if not idempotency_key.strip() or len(idempotency_key) > 255:
            raise ValueError("idempotency key must contain between 1 and 255 characters")
        if request_hash is None:
            raise HarborInvariantError("request_hash must not be None here")
        if len(request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in request_hash
        ):
            raise ValueError("request hash must be lowercase SHA-256 hexadecimal")

    async def get(self, task_id: str) -> IngestionTask | None:
        """Load one durable task without exposing workflow runtime fields."""

        if not task_id.strip():
            raise ValueError("task_id must be non-empty")
        async with self._client.sessions() as session:
            result = await session.execute(
                select(INGESTION_TASKS).where(INGESTION_TASKS.c.task_id == task_id)
            )
            row = result.mappings().one_or_none()
            return _task_from_row(row) if row is not None else None

    async def pending_submissions(self, *, limit: int = 100) -> tuple[IngestionTask, ...]:
        """Return durable PENDING tasks whose Temporal start is not confirmed."""

        if not 1 <= limit <= 1_000:
            raise ValueError("pending submission limit must be between 1 and 1000")
        submission_state = INGESTION_TASKS.c.summary["submission_state"].as_string()
        async with self._client.sessions() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(
                    INGESTION_TASKS.c.status == IngestionTaskState.PENDING.value,
                    or_(
                        submission_state.is_(None),
                        submission_state != "submitted",
                    ),
                )
                .order_by(INGESTION_TASKS.c.submitted_at, INGESTION_TASKS.c.task_id)
                .limit(limit)
            )
            return tuple(_task_from_row(row) for row in result.mappings().all())

    async def list_tasks(
        self,
        *,
        tenant_ids: frozenset[str] | None = None,
        statuses: Sequence[str] | None = None,
        before_submitted_at: datetime | None = None,
        before_task_id: str | None = None,
        limit: int = 50,
    ) -> TaskPage:
        """Newest-first keyset page of durable tasks for the public list API.

        Ordered by ``(submitted_at, task_id)`` descending so a page stays
        stable while new tasks are being registered: a task submitted after
        the caller read page one sorts ahead of the cursor rather than
        shifting rows past it. ``tenant_ids`` is the caller's authorized
        scope; None means unrestricted and is reserved for wildcard
        principals and trusted system callers.
        """

        if not 1 <= limit <= 200:
            raise ValueError("ingestion task limit must be between 1 and 200")
        if (before_submitted_at is None) != (before_task_id is None):
            raise ValueError("ingestion task cursor values must be supplied together")
        if (tenant_ids is not None and not tenant_ids) or (statuses is not None and not statuses):
            return TaskPage((), has_more=False)
        statement = select(INGESTION_TASKS)
        if tenant_ids is not None:
            statement = statement.where(INGESTION_TASKS.c.tenant_id.in_(sorted(tenant_ids)))
        if statuses is not None:
            statement = statement.where(INGESTION_TASKS.c.status.in_(tuple(statuses)))
        if before_submitted_at is not None:
            if before_task_id is None:
                raise HarborInvariantError("before_task_id must not be None here")
            statement = statement.where(
                or_(
                    INGESTION_TASKS.c.submitted_at < before_submitted_at,
                    and_(
                        INGESTION_TASKS.c.submitted_at == before_submitted_at,
                        INGESTION_TASKS.c.task_id < before_task_id,
                    ),
                )
            )
        async with self._client.sessions() as session:
            result = await session.execute(
                statement.order_by(
                    INGESTION_TASKS.c.submitted_at.desc(),
                    INGESTION_TASKS.c.task_id.desc(),
                ).limit(limit + 1)
            )
            rows = result.mappings().all()
        return TaskPage(
            items=tuple(_task_from_row(row) for row in rows[:limit]),
            has_more=len(rows) > limit,
        )

    async def list_active(
        self,
        *,
        after_submitted_at: datetime | None = None,
        after_task_id: str | None = None,
        limit: int = 500,
    ) -> tuple[IngestionTask, ...]:
        """Non-terminal tasks (PENDING/RUNNING), keyset-paginated for a progress bridge.

        Callers must page through with (submitted_at, task_id) from the last
        row of the previous page until a short page signals the end -- a
        single bounded call would silently strand tasks past the limit.
        """

        if not 1 <= limit <= 1_000:
            raise ValueError("active task limit must be between 1 and 1000")
        if (after_submitted_at is None) != (after_task_id is None):
            raise ValueError("active task cursor values must be supplied together")
        statement = select(INGESTION_TASKS).where(
            INGESTION_TASKS.c.status.in_(
                (IngestionTaskState.PENDING.value, IngestionTaskState.RUNNING.value)
            )
        )
        if after_submitted_at is not None:
            if after_task_id is None:
                raise HarborInvariantError("after_task_id must not be None here")
            statement = statement.where(
                or_(
                    INGESTION_TASKS.c.submitted_at > after_submitted_at,
                    and_(
                        INGESTION_TASKS.c.submitted_at == after_submitted_at,
                        INGESTION_TASKS.c.task_id > after_task_id,
                    ),
                )
            )
        async with self._client.sessions() as session:
            result = await session.execute(
                statement.order_by(INGESTION_TASKS.c.submitted_at, INGESTION_TASKS.c.task_id).limit(
                    limit
                )
            )
            return tuple(_task_from_row(row) for row in result.mappings().all())


def _task_from_row(row: DatabaseRow) -> IngestionTask:
    return IngestionTask(
        task_id=required_text(row, "task_id"),
        source_scope_id=required_text(row, "source_scope_id"),
        status=IngestionTaskState(required_text(row, "status")),
        submitted_at=optional_datetime(row, "submitted_at"),
        started_at=optional_datetime(row, "started_at"),
        completed_at=optional_datetime(row, "completed_at"),
        request=required_mapping(row, "request"),
        summary=required_mapping(row, "summary"),
    )
