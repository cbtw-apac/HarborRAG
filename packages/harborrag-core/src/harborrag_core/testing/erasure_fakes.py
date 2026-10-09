"""Dict-backed ``UserErasureJournal`` for tests and in-memory composition."""

from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_core.ports.erasure import UserErasureRecord


@dataclass(slots=True)
class FakeUserErasureJournal:
    records: dict[str, UserErasureRecord] = field(default_factory=dict)
    failure: Exception | None = None

    async def start(self, record: UserErasureRecord) -> None:
        if self.failure is not None:
            raise self.failure
        self.records[record.erasure_id] = record

    async def finish(self, record: UserErasureRecord) -> None:
        if record.erasure_id not in self.records:
            raise ValueError(f"user erasure {record.erasure_id} was never started")
        self.records[record.erasure_id] = record

    async def get(self, erasure_id: str) -> UserErasureRecord | None:
        return self.records.get(erasure_id)


__all__ = ["FakeUserErasureJournal"]
