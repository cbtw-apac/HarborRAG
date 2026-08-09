"""SQL canonical long-term memory adapter for the control-plane database.

``search``'s scope filter is built from ``scope_owner_fields`` -- the same
function ``harborrag_core.ports.memory.visible_to`` uses -- so the SQL
predicate and the pure in-memory predicate can never silently drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import MemoryRow
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    scope_owner_fields,
    visible_to,
)

_ALL_SCOPES = tuple(MemoryScope)


def _row_values(memory: Memory) -> dict[str, Any]:
    return {
        "scope": memory.scope.value,
        "memory_type": memory.memory_type.value,
        "tenant_id": memory.owner.tenant_id,
        "project_id": memory.owner.project_id,
        "principal_id": memory.owner.principal_id,
        "session_id": memory.owner.session_id,
        "run_id": memory.owner.run_id,
        "content": memory.content,
        "metadata_json": dict(memory.metadata),
        "importance": memory.importance,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "expires_at": memory.expires_at,
    }


def _row_to_memory(row: MemoryRow) -> Memory:
    return Memory(
        memory_id=row.memory_id,
        scope=MemoryScope(row.scope),
        memory_type=MemoryType(row.memory_type),
        owner=MemoryOwner(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            principal_id=row.principal_id,
            session_id=row.session_id,
            run_id=row.run_id,
        ),
        content=row.content,
        metadata=dict(row.metadata_json),
        importance=row.importance,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
    )


def _scope_condition(scope: MemoryScope, caller: MemoryOwner) -> sa.ColumnElement[bool]:
    """One scope's visibility predicate: matches ``visible_to`` field-for-field.

    A caller missing a field the scope requires (e.g. no ``run_id``) can
    never match a memory at that scope, mirroring ``visible_to``'s behavior
    for the same case.
    """

    conditions: list[sa.ColumnElement[bool]] = [MemoryRow.scope == scope.value]
    for name in scope_owner_fields(scope):
        value = getattr(caller, name)
        if value is None:
            return sa.false()
        conditions.append(getattr(MemoryRow, name) == value)
    return sa.and_(*conditions)


def _visibility_filter(query: MemoryQuery) -> sa.ColumnElement[bool]:
    scopes = query.scopes or _ALL_SCOPES
    return sa.or_(*(_scope_condition(scope, query.owner) for scope in scopes))


@dataclass(slots=True)
class SqlMemoryRepository:
    """Persist canonical long-term memories through async SQLAlchemy."""

    sessions: SessionFactory

    async def save(self, memory: Memory) -> None:
        async with self.sessions.begin() as session:
            existing = await session.get(MemoryRow, memory.memory_id)
            if existing is None:
                session.add(MemoryRow(memory_id=memory.memory_id, **_row_values(memory)))
                return
            stored = _row_to_memory(existing)
            if stored.scope is not memory.scope or stored.owner != memory.owner:
                raise HarborConflictError("memory identity belongs to a different owner or scope")
            values = _row_values(memory)
            values.pop("created_at")
            for key, value in values.items():
                setattr(existing, key, value)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        async with self.sessions() as session:
            row = await session.get(MemoryRow, memory_id)
        if row is None:
            return None
        memory = _row_to_memory(row)
        if memory.expires_at is not None and memory.expires_at <= utc_now():
            return None
        if not visible_to(memory.scope, memory.owner, caller):
            return None
        return memory

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        now = utc_now()
        statement = sa.select(MemoryRow).where(
            _visibility_filter(query),
            sa.or_(MemoryRow.expires_at.is_(None), MemoryRow.expires_at > now),
        )
        if query.memory_types:
            statement = statement.where(
                MemoryRow.memory_type.in_(mtype.value for mtype in query.memory_types)
            )
        if query.text:
            statement = statement.where(MemoryRow.content.icontains(query.text))
        statement = statement.order_by(
            MemoryRow.importance.desc(), MemoryRow.created_at.desc()
        ).limit(query.limit)
        async with self.sessions() as session:
            rows = list(await session.scalars(statement))
        return tuple(_row_to_memory(row) for row in rows)

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(MemoryRow, memory_id)
            if row is None:
                return
            if not visible_to(MemoryScope(row.scope), _row_to_memory(row).owner, caller):
                return
            await session.delete(row)


__all__ = ["SqlMemoryRepository"]
