"""ActivityEntry: one append-only audit row, written on every mutation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def _utc_now() -> datetime:
    """Timezone-aware UTC timestamp (RFC 3339-ready, per API conventions)."""
    return datetime.now(UTC)


@dataclass(slots=True)
class ActivityEntry:
    """Who did what to which entity; summaries must never contain secrets."""

    id: str
    actor: str
    verb: str
    entity_type: str
    entity_id: str
    summary: str
    created_at: datetime = field(default_factory=_utc_now)
