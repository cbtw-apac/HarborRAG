"""SqlGraphConflictRepository: GraphConflictRepositoryPort over graph_conflicts."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.domain.graph_conflict import ConflictAction, ConflictStatus, GraphConflict
from harborrag_core.invariants import HarborInvariantError

from .mapping import utc_now
from .schemas import GraphConflictRow
from .session import SessionFactory


@dataclass(slots=True)
class SqlGraphConflictRepository:
    """GraphConflictRepositoryPort over the graph_conflicts table."""

    sessions: SessionFactory

    async def report(self, conflict: GraphConflict) -> GraphConflict:
        """Durably record a newly detected conflict."""
        async with self.sessions.begin() as session:
            session.add(
                GraphConflictRow(
                    id=conflict.id,
                    tenant_id=conflict.tenant_id,
                    conflict_type=conflict.conflict_type,
                    subject_node_key=conflict.subject_node_key,
                    competing_node_key=conflict.competing_node_key,
                    description=conflict.description,
                    status=conflict.status,
                    action=conflict.action,
                    resolved_by=conflict.resolved_by,
                    detected_at=conflict.detected_at,
                    resolved_at=conflict.resolved_at,
                )
            )
        return conflict

    async def list(
        self,
        *,
        tenant_ids: frozenset[str] | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[GraphConflict], str | None]:
        """Newest-detected first within ``tenant_ids``, walked via an opaque keyset cursor."""
        statement = sa.select(GraphConflictRow).order_by(
            GraphConflictRow.detected_at.desc(), GraphConflictRow.id.desc()
        )
        if tenant_ids is not None:
            statement = statement.where(GraphConflictRow.tenant_id.in_(tenant_ids))
        if cursor is not None:
            detected_at, conflict_id = _decode_cursor(cursor)
            statement = statement.where(
                sa.or_(
                    GraphConflictRow.detected_at < detected_at,
                    sa.and_(
                        GraphConflictRow.detected_at == detected_at,
                        GraphConflictRow.id < conflict_id,
                    ),
                )
            )
        statement = statement.limit(limit + 1)
        async with self.sessions() as session:
            rows = list(await session.scalars(statement))
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = _encode_cursor(page[-1].detected_at, page[-1].id) if has_more else None
        return [self._to_domain(row) for row in page], next_cursor

    async def get(
        self, conflict_id: str, *, tenant_ids: frozenset[str] | None
    ) -> GraphConflict | None:
        """One conflict by id within ``tenant_ids``, or None."""
        async with self.sessions() as session:
            row = await session.get(GraphConflictRow, conflict_id)
            if row is None or (tenant_ids is not None and row.tenant_id not in tenant_ids):
                return None
            return self._to_domain(row)

    async def resolve(
        self,
        conflict_id: str,
        *,
        action: ConflictAction,
        resolved_by: str,
        tenant_ids: frozenset[str] | None,
    ) -> GraphConflict:
        """Close a conflict with the chosen action; record-only, no graph mutation.

        Not a read-then-write: the status flip is one conditional UPDATE (WHERE
        status='open'), evaluated atomically by the database, so two requests
        racing to resolve the same conflict can never both read "open" and both
        commit -- exactly one UPDATE matches a row. The loser falls back to a
        scoped read to tell 404 (missing or wrong tenant) apart from 409
        (already resolved), mirroring SqlLeaseRepository.try_acquire.
        """
        async with self.sessions.begin() as session:
            statement = sa.update(GraphConflictRow).where(
                GraphConflictRow.id == conflict_id, GraphConflictRow.status == "open"
            )
            if tenant_ids is not None:
                statement = statement.where(GraphConflictRow.tenant_id.in_(tenant_ids))
            statement = statement.values(
                status="resolved",
                action=action,
                resolved_by=resolved_by,
                resolved_at=utc_now(),
            )
            result = cast("CursorResult[Any]", await session.execute(statement))
            if result.rowcount == 1:
                row = await session.get(GraphConflictRow, conflict_id)
                if row is None:
                    raise HarborInvariantError(
                        f"graph conflict {conflict_id!r} vanished mid-transaction"
                    )
                return self._to_domain(row)

            row = await session.get(GraphConflictRow, conflict_id)
            if row is None or (tenant_ids is not None and row.tenant_id not in tenant_ids):
                raise HarborNotFoundError(f"graph conflict {conflict_id!r} not found")
            raise HarborConflictError(f"graph conflict {conflict_id!r} is already resolved")

    @staticmethod
    def _to_domain(row: GraphConflictRow) -> GraphConflict:
        """Map a graph_conflicts row to the GraphConflict aggregate."""
        return GraphConflict(
            id=row.id,
            tenant_id=row.tenant_id,
            conflict_type=row.conflict_type,
            subject_node_key=row.subject_node_key,
            competing_node_key=row.competing_node_key,
            description=row.description,
            status=cast(ConflictStatus, row.status),
            action=cast("ConflictAction | None", row.action),
            resolved_by=row.resolved_by,
            detected_at=row.detected_at,
            resolved_at=row.resolved_at,
        )


def _encode_cursor(detected_at: datetime, conflict_id: str) -> str:
    payload = json.dumps(
        {"detected_at": detected_at.isoformat(), "id": conflict_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    padding = "=" * (-len(value) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        detected_at = datetime.fromisoformat(str(payload["detected_at"]))
        conflict_id = str(payload["id"])
        if not conflict_id:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HarborValidationError("graph conflict cursor is invalid") from error
    return detected_at, conflict_id
