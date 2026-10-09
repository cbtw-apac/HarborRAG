"""Durable journal of right-to-erasure runs: who erased whom, when, and what went.

One row per run, written ``running`` before any store is touched and finished
afterwards, so a crash or a failed step is visible and the same request can
simply be re-run. Rows hold identifiers and counts only, never content.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import uuid4

from harborrag_core.base import utc_now

UserErasureStatus = Literal["running", "completed", "deferred", "failed"]
"""``deferred``: every store was erased; some vector deletes are queued for retry."""


def new_erasure_id() -> str:
    """Generate one opaque erasure-run identifier."""

    return f"erasure-{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class UserErasureRecord:
    """One erasure run. ``counts`` maps a store name to the rows it removed."""

    tenant_id: str
    subject_user_id: str
    actor: str
    status: UserErasureStatus = "running"
    counts: Mapping[str, int] = field(default_factory=dict)
    failed_steps: tuple[str, ...] = ()
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    erasure_id: str = field(default_factory=new_erasure_id)

    def __post_init__(self) -> None:
        for name in ("tenant_id", "subject_user_id", "actor"):
            if not getattr(self, name).strip():
                raise ValueError(f"user erasure {name} must be non-blank")
        if any(value < 0 for value in self.counts.values()):
            raise ValueError("user erasure counts must not be negative")
        object.__setattr__(self, "counts", dict(self.counts))


class UserErasureJournal(Protocol):
    """Persistence-neutral journal of user-erasure runs."""

    async def start(self, record: UserErasureRecord) -> None:
        """Persist ``record`` before any deletion; raise when it cannot be written."""
        ...

    async def finish(self, record: UserErasureRecord) -> None:
        """Overwrite the started row with ``record``; ``ValueError`` if never started."""
        ...

    async def get(self, erasure_id: str) -> UserErasureRecord | None: ...


__all__ = ["UserErasureJournal", "UserErasureRecord", "UserErasureStatus", "new_erasure_id"]
