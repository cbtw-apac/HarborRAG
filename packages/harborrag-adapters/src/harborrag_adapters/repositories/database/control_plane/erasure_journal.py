"""SqlUserErasureJournal: UserErasureJournal over the user_erasures table."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from harborrag_core.ports.erasure import UserErasureRecord, UserErasureStatus

from .schemas_erasure import UserErasureRow
from .session import SessionFactory


def _values(record: UserErasureRecord) -> dict[str, Any]:
    return {
        "tenant_id": record.tenant_id,
        "subject_user_id": record.subject_user_id,
        "actor": record.actor,
        "status": record.status,
        "counts_json": dict(record.counts),
        "failed_steps_json": list(record.failed_steps),
        "started_at": record.started_at,
        "finished_at": record.finished_at,
    }


def _record(row: UserErasureRow) -> UserErasureRecord:
    return UserErasureRecord(
        erasure_id=row.erasure_id,
        tenant_id=row.tenant_id,
        subject_user_id=row.subject_user_id,
        actor=row.actor,
        status=cast(UserErasureStatus, row.status),
        counts={str(name): int(value) for name, value in row.counts_json.items()},
        failed_steps=tuple(str(step) for step in row.failed_steps_json),
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


@dataclass(slots=True)
class SqlUserErasureJournal:
    """UserErasureJournal over user_erasures."""

    sessions: SessionFactory

    async def start(self, record: UserErasureRecord) -> None:
        async with self.sessions.begin() as session:
            session.add(UserErasureRow(erasure_id=record.erasure_id, **_values(record)))

    async def finish(self, record: UserErasureRecord) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(UserErasureRow, record.erasure_id)
            if row is None:
                raise ValueError(f"user erasure {record.erasure_id} was never started")
            for name, value in _values(record).items():
                setattr(row, name, value)

    async def get(self, erasure_id: str) -> UserErasureRecord | None:
        async with self.sessions() as session:
            row = await session.get(UserErasureRow, erasure_id)
        return None if row is None else _record(row)


__all__ = ["SqlUserErasureJournal"]
