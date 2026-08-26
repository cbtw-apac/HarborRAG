"""ActivityEntry: one append-only audit row, written on every mutation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from harborrag_core.base import utc_now

from .validation import require_id, require_tenant_id


@dataclass(slots=True)
class ActivityEntry:
    """Who did what to which entity; summaries must never contain secrets."""

    id: str
    actor: str
    verb: str
    entity_type: str
    entity_id: str
    summary: str
    tenant_id: str
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_id(self.id, label="Activity")
        require_tenant_id(self.tenant_id)
