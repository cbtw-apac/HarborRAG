"""FakeGraphConflictRepository: split out of control_plane_fakes.py to keep that
file under the repo's file-length gate.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborConflictError, HarborNotFoundError
from harborrag_core.domain.graph_conflict import ConflictAction, GraphConflict


def _in_scope(tenant_id: str, tenant_ids: frozenset[str] | None) -> bool:
    """Mirror the Sql* repositories' tenant filter: None means unrestricted."""
    return tenant_ids is None or tenant_id in tenant_ids


@dataclass(slots=True)
class FakeGraphConflictRepository:
    """Dict-backed GraphConflictRepositoryPort; mirrors the SQL adapter's keyset cursor."""

    conflicts: dict[str, GraphConflict] = field(default_factory=dict)

    async def report(self, conflict: GraphConflict) -> GraphConflict:
        """Record a newly detected conflict."""
        self.conflicts[conflict.id] = conflict
        return conflict

    async def list(
        self,
        *,
        tenant_ids: frozenset[str] | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[GraphConflict], str | None]:
        """Newest-detected first within ``tenant_ids``, walked via an opaque keyset cursor."""
        scoped = [c for c in self.conflicts.values() if _in_scope(c.tenant_id, tenant_ids)]
        ordered = sorted(scoped, key=lambda c: (c.detected_at, c.id), reverse=True)
        if cursor is not None:
            position = _decode_conflict_cursor(cursor)
            ordered = [item for item in ordered if (item.detected_at, item.id) < position]
        page = ordered[:limit]
        next_cursor = (
            _encode_conflict_cursor(page[-1].detected_at, page[-1].id)
            if len(page) == limit and len(ordered) > limit
            else None
        )
        return page, next_cursor

    async def get(
        self, conflict_id: str, *, tenant_ids: frozenset[str] | None
    ) -> GraphConflict | None:
        """Conflict by id within ``tenant_ids``, or None."""
        conflict = self.conflicts.get(conflict_id)
        if conflict is None or not _in_scope(conflict.tenant_id, tenant_ids):
            return None
        return conflict

    async def resolve(
        self,
        conflict_id: str,
        *,
        action: ConflictAction,
        resolved_by: str,
        tenant_ids: frozenset[str] | None,
    ) -> GraphConflict:
        """Close a conflict with the chosen action."""
        conflict = await self.get(conflict_id, tenant_ids=tenant_ids)
        if conflict is None:
            raise HarborNotFoundError(f"graph conflict {conflict_id!r} not found")
        if conflict.status == "resolved":
            raise HarborConflictError(f"graph conflict {conflict_id!r} is already resolved")
        conflict.status = "resolved"
        conflict.action = action
        conflict.resolved_by = resolved_by
        conflict.resolved_at = utc_now()
        return conflict


def _encode_conflict_cursor(detected_at: datetime, conflict_id: str) -> str:
    payload = json.dumps(
        {"detected_at": detected_at.isoformat(), "id": conflict_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_conflict_cursor(value: str) -> tuple[datetime, str]:
    padding = "=" * (-len(value) % 4)
    payload = json.loads(base64.urlsafe_b64decode(value + padding))
    return datetime.fromisoformat(payload["detected_at"]), str(payload["id"])
