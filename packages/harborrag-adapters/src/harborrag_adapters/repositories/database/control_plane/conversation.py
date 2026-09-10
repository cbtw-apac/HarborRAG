"""SQL conversation-history adapter for the control-plane database.

Messages are the only thing persisted (``conversation_messages``); the
completed-turn view (``recent``/``append``) is derived from them with
``turns_from_messages`` so both views always agree.

Every predicate is scoped by ``(tenant_id, user_id, session_id)``: the human
owns the conversation. ``principal_id`` is still written and returned, but
only as the credential that acted -- it is never load-bearing on its own, or
one service principal fronting several people would leave ``session_id`` as
the sole separator between their histories.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from harborrag_adapters.repositories.database.control_plane import conversation_directory
from harborrag_adapters.repositories.database.control_plane.conversation_directory import (
    ConversationListRequest,
)
from harborrag_adapters.repositories.database.control_plane.mapping import utc_now
from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    ConversationMessageRow,
    ConversationSessionRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationKind,
    ConversationMessage,
    ConversationPage,
    ConversationTurn,
    new_message_id,
    normalize_conversation_title,
    turns_from_messages,
)


def _identity_filter(identity: ConversationIdentity) -> tuple[ColumnElement[bool], ...]:
    return (
        ConversationMessageRow.tenant_id == identity.tenant_id,
        ConversationMessageRow.user_id == identity.user_id,
        ConversationMessageRow.session_id == identity.session_id,
    )


def _session_filter(identity: ConversationIdentity) -> tuple[ColumnElement[bool], ...]:
    return (
        *conversation_directory.owner_filter(identity.tenant_id, identity.user_id),
        ConversationSessionRow.session_id == identity.session_id,
    )


_NEWEST_FIRST = (
    ConversationMessageRow.seq.desc(),
    ConversationMessageRow.created_at.desc(),
    ConversationMessageRow.message_id.desc(),
)
_OLDEST_FIRST = (
    ConversationMessageRow.seq.asc(),
    ConversationMessageRow.created_at.asc(),
    ConversationMessageRow.message_id.asc(),
)


def _row_to_message(row: ConversationMessageRow) -> ConversationMessage:
    return ConversationMessage(
        message_id=row.message_id,
        role=row.role,  # type: ignore[arg-type]
        content=row.content,
        created_at=row.created_at,
        token_count=row.token_count,
        tool_calls_json=row.tool_calls_json,
        tool_call_id=row.tool_call_id,
        citations_json=row.citations_json,
        run_id=row.run_id,
        partial=row.partial,
    )


def _message_row(
    identity: ConversationIdentity, message: ConversationMessage, seq: int
) -> ConversationMessageRow:
    return ConversationMessageRow(
        message_id=message.message_id,
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        user_id=identity.user_id,
        session_id=identity.session_id,
        role=message.role,
        content=message.content,
        token_count=message.token_count,
        tool_calls_json=message.tool_calls_json,
        tool_call_id=message.tool_call_id,
        citations_json=message.citations_json,
        run_id=message.run_id,
        partial=message.partial,
        created_at=message.created_at,
        seq=seq,
    )


async def _next_seq(session: AsyncSession, identity: ConversationIdentity) -> int:
    current = await session.scalar(
        sa.select(sa.func.max(ConversationMessageRow.seq)).where(*_identity_filter(identity))
    )
    return int(current or 0) + 1


def _require_positive(limit: int) -> None:
    if limit < 1:
        raise ValueError("conversation memory limit must be positive")


@dataclass(slots=True)
class SqlConversationMemoryRepository:
    """Persist conversation sessions and messages through async SQLAlchemy."""

    sessions: SessionFactory

    async def create(
        self,
        identity: ConversationIdentity,
        *,
        kind: ConversationKind = "chat",
        title: str | None = None,
    ) -> None:
        now = utc_now()
        async with self.sessions.begin() as session:
            session.add(
                ConversationSessionRow(
                    tenant_id=identity.tenant_id,
                    principal_id=identity.principal_id,
                    user_id=identity.user_id,
                    session_id=identity.session_id,
                    kind=kind,
                    title=normalize_conversation_title(title),
                    created_at=now,
                    updated_at=now,
                )
            )

    async def exists(
        self,
        identity: ConversationIdentity,
        *,
        kind: ConversationKind | None = None,
    ) -> bool:
        conditions: list[ColumnElement[bool]] = list(_session_filter(identity))
        if kind is not None:
            conditions.append(ConversationSessionRow.kind == kind)
        statement = sa.select(sa.exists().where(*conditions))
        async with self.sessions() as session:
            return bool(await session.scalar(statement))

    async def delete(self, identity: ConversationIdentity) -> bool:
        """Delete the caller's own session row; messages cascade with it."""

        async with self.sessions.begin() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    sa.delete(ConversationSessionRow).where(*_session_filter(identity))
                ),
            )
            return result.rowcount > 0

    # -- conversation directory ----------------------------------------------

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        kind: ConversationKind | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ConversationPage:
        request = ConversationListRequest(
            tenant_id=tenant_id,
            user_id=user_id,
            kind=kind,
            cursor=cursor,
            limit=limit,
        )
        async with self.sessions() as session:
            return await conversation_directory.list_conversations(session, request)

    async def rename_conversation(
        self,
        identity: ConversationIdentity,
        *,
        title: str,
    ) -> bool:
        async with self.sessions.begin() as session:
            return await conversation_directory.rename_conversation(session, identity, title)

    # -- per-message history -------------------------------------------------

    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None:
        if not messages:
            return
        async with self.sessions.begin() as session:
            seq = await _next_seq(session, identity)
            for offset, message in enumerate(messages):
                session.add(_message_row(identity, message, seq + offset))
            # Same transaction as the append, and user-scoped: a conversation
            # listing sorts by real activity, and no caller can bump a row
            # that is not theirs.
            await session.execute(
                sa.update(ConversationSessionRow)
                .where(*_session_filter(identity))
                .values(updated_at=utc_now())
            )

    async def recent_messages(
        self,
        identity: ConversationIdentity,
        *,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        _require_positive(limit)
        statement = (
            sa.select(ConversationMessageRow)
            .where(*_identity_filter(identity))
            .order_by(*_NEWEST_FIRST)
            .limit(limit)
        )
        async with self.sessions() as session:
            rows = list(await session.scalars(statement))
        rows.reverse()
        return tuple(_row_to_message(row) for row in rows)

    async def messages_after(
        self,
        identity: ConversationIdentity,
        *,
        after_message_id: str | None,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        _require_positive(limit)
        statement = sa.select(ConversationMessageRow).where(*_identity_filter(identity))
        async with self.sessions() as session:
            if after_message_id is not None:
                anchor = await session.scalar(
                    sa.select(ConversationMessageRow.seq).where(
                        *_identity_filter(identity),
                        ConversationMessageRow.message_id == after_message_id,
                    )
                )
                if anchor is None:
                    raise ValueError("unknown conversation message cursor")
                statement = statement.where(ConversationMessageRow.seq > anchor)
            rows = await session.scalars(statement.order_by(*_OLDEST_FIRST).limit(limit))
            return tuple(_row_to_message(row) for row in rows)

    async def clear_messages(self, identity: ConversationIdentity) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                sa.delete(ConversationMessageRow).where(*_identity_filter(identity))
            )

    # -- derived completed-turn view -----------------------------------------

    async def recent(
        self,
        identity: ConversationIdentity,
        *,
        limit: int = 2,
    ) -> tuple[ConversationTurn, ...]:
        _require_positive(limit)
        statement = (
            sa.select(ConversationMessageRow)
            .where(
                *_identity_filter(identity),
                ConversationMessageRow.role.in_(("user", "assistant")),
            )
            .order_by(*_NEWEST_FIRST)
        )
        collected: list[ConversationMessage] = []
        async with self.sessions() as session:
            result = await session.stream_scalars(statement)
            try:
                pairs = 0
                awaiting_user = False
                async for row in result:
                    collected.append(_row_to_message(row))
                    # Newest-first twin of turns_from_messages: an assistant
                    # message completes a pair with the nearest earlier user.
                    if row.role == "assistant":
                        awaiting_user = True
                    elif awaiting_user:
                        awaiting_user = False
                        pairs += 1
                        if pairs >= limit:
                            break
            finally:
                await result.close()
        collected.reverse()
        return turns_from_messages(collected)[-limit:]

    async def append(
        self,
        identity: ConversationIdentity,
        turn: ConversationTurn,
    ) -> None:
        now = utc_now()
        await self.append_messages(
            identity,
            (
                ConversationMessage(new_message_id(), "user", turn.user_content, now),
                ConversationMessage(new_message_id(), "assistant", turn.assistant_content, now),
            ),
        )

    async def clear(self, identity: ConversationIdentity) -> None:
        await self.clear_messages(identity)


__all__ = ["SqlConversationMemoryRepository"]
