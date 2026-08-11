from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.contracts.events import HarborEvent

from .schema import INGESTION_TASKS, TASK_EVENTS


@dataclass(slots=True)
class TaskEventRepository:
    """Ordered per-task event log (WS/SSE reconnect replay source).

    Mirrors SqlJobRepository.append_event's atomic sequence claim: the
    counter update is one statement, so concurrent appenders cannot observe
    and allocate the same sequence number.
    """

    client: SQLAlchemyDBClient

    async def append_event(self, task_id: str, event: HarborEvent) -> None:
        """Append the event with the next per-task sequence number."""
        async with self.client.sessions.begin() as session:
            result = await session.execute(
                sa.update(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .values(event_sequence=INGESTION_TASKS.c.event_sequence + 1)
                .returning(INGESTION_TASKS.c.event_sequence)
            )
            next_seq = result.scalar_one_or_none()
            if next_seq is None:
                raise HarborNotFoundError(f"ingestion task does not exist: {task_id}")
            await session.execute(
                sa.insert(TASK_EVENTS).values(
                    task_id=task_id,
                    seq=next_seq,
                    name=event.name,
                    trace_id=event.trace_id,
                    payload_json=dict(event.payload),
                    created_at=event.created_at,
                )
            )

    async def list_events(
        self,
        task_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 500,
    ) -> list[HarborEvent]:
        """The task's event log in append order, bounded by ``limit``.

        ``after_seq`` resumes a paged read (e.g. after an earlier bounded
        page) rather than always replaying the full backlog.
        """
        statement = (
            sa.select(TASK_EVENTS)
            .where(TASK_EVENTS.c.task_id == task_id)
            .order_by(TASK_EVENTS.c.seq)
            .limit(limit)
        )
        if after_seq is not None:
            statement = statement.where(TASK_EVENTS.c.seq > after_seq)
        async with self.client.sessions() as session:
            rows = (await session.execute(statement)).mappings().all()
            return [
                HarborEvent(
                    name=row["name"],
                    trace_id=row["trace_id"],
                    payload=dict(row["payload_json"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]
