"""Conversation listing and renaming queries for the control-plane database.

Split out of ``conversation.py`` (file-length gate) but part of the same
adapter: ``SqlConversationMemoryRepository`` delegates its
``ConversationDirectory`` half here. Every predicate is scoped by
``(tenant_id, user_id)`` -- the human owns the conversation, the credential
that acted does not.

Paging is keyset, not offset: the opaque cursor carries only a session id,
whose row supplies the ``updated_at`` anchor. That mirrors how
``messages_after`` resolves a message-id cursor to a ``seq``, and it means a
cursor for a conversation the caller does not own is rejected rather than
silently treated as the start of the listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    ConversationMessageRow,
    ConversationSessionRow,
)
from harborrag_core.ports.conversation import (
    CONVERSATION_CURSOR_ERROR,
    MAX_CONVERSATION_PAGE_LIMIT,
    ConversationIdentity,
    ConversationKind,
    ConversationPage,
    ConversationSummaryRow,
    decode_conversation_cursor,
    encode_conversation_cursor,
    normalize_conversation_title,
)

_NEWEST_ACTIVITY_FIRST = (
    ConversationSessionRow.updated_at.desc(),
    ConversationSessionRow.session_id.desc(),
)

_MESSAGE_COUNT = (
    sa.select(sa.func.count())
    .select_from(ConversationMessageRow)
    .where(ConversationMessageRow.session_id == ConversationSessionRow.session_id)
    .scalar_subquery()
)


@dataclass(frozen=True, slots=True)
class ConversationListRequest:
    """Bundled filters for one page of a user's conversations."""

    tenant_id: str
    user_id: str
    kind: ConversationKind | None = None
    cursor: str | None = None
    limit: int = 20

    @property
    def page_size(self) -> int:
        """The requested limit clamped to the port's 1..100 window."""

        return max(1, min(self.limit, MAX_CONVERSATION_PAGE_LIMIT))


def owner_filter(tenant_id: str, user_id: str) -> tuple[ColumnElement[bool], ...]:
    """Predicate restricting conversation sessions to one human."""

    return (
        ConversationSessionRow.tenant_id == tenant_id,
        ConversationSessionRow.user_id == user_id,
    )


async def _anchor(
    session: AsyncSession,
    request: ConversationListRequest,
    session_id: str,
) -> ColumnElement[bool]:
    """Resolve the cursor's session to its ``updated_at`` keyset predicate."""

    anchor = await session.scalar(
        sa.select(ConversationSessionRow.updated_at).where(
            *owner_filter(request.tenant_id, request.user_id),
            ConversationSessionRow.session_id == session_id,
        )
    )
    if anchor is None:
        raise ValueError(CONVERSATION_CURSOR_ERROR)
    return sa.or_(
        ConversationSessionRow.updated_at < anchor,
        sa.and_(
            ConversationSessionRow.updated_at == anchor,
            ConversationSessionRow.session_id < session_id,
        ),
    )


def _summary(row: sa.Row[tuple[ConversationSessionRow, int]]) -> ConversationSummaryRow:
    stored, message_count = row
    kind: ConversationKind = "agent" if stored.kind == "agent" else "chat"
    return ConversationSummaryRow(
        session_id=stored.session_id,
        kind=kind,
        title=stored.title,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        message_count=int(message_count),
    )


async def list_conversations(
    session: AsyncSession,
    request: ConversationListRequest,
) -> ConversationPage:
    """Return one keyset page of the user's conversations, newest activity first."""

    conditions: list[ColumnElement[bool]] = list(owner_filter(request.tenant_id, request.user_id))
    if request.kind is not None:
        conditions.append(ConversationSessionRow.kind == request.kind)
    if request.cursor is not None:
        anchor_id = decode_conversation_cursor(request.cursor)
        conditions.append(await _anchor(session, request, anchor_id))
    size = request.page_size
    statement = (
        sa.select(ConversationSessionRow, _MESSAGE_COUNT)
        .where(*conditions)
        .order_by(*_NEWEST_ACTIVITY_FIRST)
        .limit(size + 1)
    )
    rows = (await session.execute(statement)).all()
    page = tuple(_summary(row) for row in rows[:size])
    has_more = len(rows) > size and bool(page)
    next_cursor = encode_conversation_cursor(page[-1].session_id) if has_more else None
    return ConversationPage(conversations=page, next_cursor=next_cursor)


async def rename_conversation(
    session: AsyncSession,
    identity: ConversationIdentity,
    title: str,
) -> bool:
    """Retitle the caller's conversation; ``False`` when it is not theirs."""

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            sa.update(ConversationSessionRow)
            .where(
                *owner_filter(identity.tenant_id, identity.user_id),
                ConversationSessionRow.session_id == identity.session_id,
            )
            .values(title=normalize_conversation_title(title))
        ),
    )
    return result.rowcount > 0


__all__ = [
    "ConversationListRequest",
    "list_conversations",
    "owner_filter",
    "rename_conversation",
]
