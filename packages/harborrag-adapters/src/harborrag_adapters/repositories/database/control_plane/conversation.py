"""SQL conversation-memory adapter for the control-plane database."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.mapping import utc_now
from harborrag_adapters.repositories.database.control_plane.schemas import (
    ConversationMemoryRow,
    ConversationSessionRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.conversation import ConversationIdentity, ConversationTurn


def _identity_filter(identity: ConversationIdentity) -> tuple[object, ...]:
    return (
        ConversationMemoryRow.tenant_id == identity.tenant_id,
        ConversationMemoryRow.principal_id == identity.principal_id,
        ConversationMemoryRow.session_id == identity.session_id,
    )


@dataclass(slots=True)
class SqlConversationMemoryRepository:
    """Persist completed turns in PostgreSQL through async SQLAlchemy."""

    sessions: SessionFactory

    async def create(self, identity: ConversationIdentity) -> None:
        async with self.sessions.begin() as session:
            session.add(
                ConversationSessionRow(
                    tenant_id=identity.tenant_id,
                    principal_id=identity.principal_id,
                    session_id=identity.session_id,
                    created_at=utc_now(),
                )
            )

    async def exists(self, identity: ConversationIdentity) -> bool:
        statement = sa.select(
            sa.exists().where(
                ConversationSessionRow.tenant_id == identity.tenant_id,
                ConversationSessionRow.principal_id == identity.principal_id,
                ConversationSessionRow.session_id == identity.session_id,
            )
        )
        async with self.sessions() as session:
            return bool(await session.scalar(statement))

    async def recent(
        self,
        identity: ConversationIdentity,
        *,
        limit: int = 2,
    ) -> tuple[ConversationTurn, ...]:
        if limit < 1:
            raise ValueError("conversation memory limit must be positive")
        statement = (
            sa.select(ConversationMemoryRow)
            .where(*_identity_filter(identity))
            .order_by(ConversationMemoryRow.created_at.desc(), ConversationMemoryRow.id.desc())
            .limit(limit)
        )
        async with self.sessions() as session:
            rows = list(await session.scalars(statement))
        rows.reverse()
        return tuple(
            ConversationTurn(
                user_content=row.user_content,
                assistant_content=row.assistant_content,
            )
            for row in rows
        )

    async def append(
        self,
        identity: ConversationIdentity,
        turn: ConversationTurn,
    ) -> None:
        async with self.sessions.begin() as session:
            session.add(
                ConversationMemoryRow(
                    tenant_id=identity.tenant_id,
                    principal_id=identity.principal_id,
                    session_id=identity.session_id,
                    user_content=turn.user_content,
                    assistant_content=turn.assistant_content,
                    created_at=utc_now(),
                )
            )

    async def clear(self, identity: ConversationIdentity) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                sa.delete(ConversationMemoryRow).where(*_identity_filter(identity))
            )


__all__ = ["SqlConversationMemoryRepository"]
