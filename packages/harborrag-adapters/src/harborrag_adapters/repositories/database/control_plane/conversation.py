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
from dataclasses import dataclass, field
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql.elements import ColumnElement

from harborrag_adapters.repositories.database.control_plane import (
    conversation_directory,
    conversation_leases,
)
from harborrag_adapters.repositories.database.control_plane.completion_requests import (
    SqlCompletionRequestStore,
)
from harborrag_adapters.repositories.database.control_plane.conversation_directory import (
    ConversationListRequest,
)
from harborrag_adapters.repositories.database.control_plane.mapping import utc_now
from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    AgentRunRow,
    ConversationMemoryLegacyRow,
    ConversationMessageRow,
    ConversationSessionRow,
)
from harborrag_adapters.repositories.database.control_plane.schemas_completion_requests import (
    CompletionRequestRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.completion_requests import CompletionClaim
from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationKind,
    ConversationMessage,
    ConversationPage,
    ConversationTurn,
    is_complete_reply,
    new_message_id,
    normalize_conversation_title,
    turns_from_messages,
)
from harborrag_core.ports.conversation_leases import ConversationLeaseContext


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


def _require_positive(limit: int) -> None:
    if limit < 1:
        raise ValueError("conversation memory limit must be positive")


@dataclass(slots=True)
class SqlConversationMemoryRepository:
    """Persist conversation sessions and messages through async SQLAlchemy."""

    sessions: SessionFactory
    _lease_context: ConversationLeaseContext = field(
        default_factory=ConversationLeaseContext, init=False
    )

    async def claim_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim:
        return await SqlCompletionRequestStore(self.sessions).claim_completion(
            tenant_id=tenant_id, user_id=user_id, key=key, request_hash=request_hash
        )

    async def finish_completion(  # noqa: PLR0913 - mirrors the scoped idempotency port
        self,
        *,
        tenant_id: str,
        user_id: str,
        key: str,
        request_hash: str,
        response_json: str | None,
        session_id: str | None = None,
    ) -> None:
        await SqlCompletionRequestStore(self.sessions).finish_completion(
            tenant_id=tenant_id,
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            response_json=response_json,
            session_id=session_id,
        )

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
                    title_source="manual" if normalize_conversation_title(title) else None,
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
        """Delete the owned session and dependent history in one transaction."""

        async with self.sessions.begin() as session:
            if not await conversation_leases.lock_for_write(
                session, identity, self._lease_context.token(identity)
            ):
                return False
            # SQLite connections do not universally enable FK enforcement.
            # Explicit deletes give erasure the same semantics on every backend.
            await session.execute(
                sa.delete(ConversationMessageRow).where(
                    ConversationMessageRow.session_id == identity.session_id
                )
            )
            await session.execute(
                sa.delete(ConversationMemoryLegacyRow).where(
                    ConversationMemoryLegacyRow.session_id == identity.session_id,
                )
            )
            await session.execute(
                sa.delete(AgentRunRow).where(
                    AgentRunRow.session_id == identity.session_id,
                )
            )
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    sa.delete(ConversationSessionRow).where(*_session_filter(identity))
                ),
            )
            await session.execute(
                sa.update(CompletionRequestRow)
                .where(
                    CompletionRequestRow.tenant_id == identity.tenant_id,
                    CompletionRequestRow.user_id == identity.user_id,
                    CompletionRequestRow.session_id == identity.session_id,
                )
                .values(status="failed", response_json=None, updated_at=utc_now())
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

    async def set_generated_title(self, identity: ConversationIdentity, *, title: str) -> bool:
        async with self.sessions.begin() as session:
            if not await conversation_leases.lock_for_write(
                session, identity, self._lease_context.token(identity)
            ):
                return False
            return await conversation_directory.set_generated_title(session, identity, title)

    async def get_title(self, identity: ConversationIdentity) -> str | None:
        async with self.sessions() as session:
            return await session.scalar(
                sa.select(ConversationSessionRow.title).where(*_session_filter(identity))
            )

    async def acquire_turn_lease(
        self, identity: ConversationIdentity, *, token: str, lease_seconds: float
    ) -> bool:
        async with self.sessions.begin() as session:
            acquired = await conversation_leases.claim_lease(
                session, identity, token=token, lease_seconds=lease_seconds, renew=False
            )
        if acquired:
            self._lease_context.bind(identity, token)
        return acquired

    async def renew_turn_lease(
        self, identity: ConversationIdentity, *, token: str, lease_seconds: float
    ) -> bool:
        async with self.sessions.begin() as session:
            return await conversation_leases.claim_lease(
                session, identity, token=token, lease_seconds=lease_seconds, renew=True
            )

    async def release_turn_lease(self, identity: ConversationIdentity, *, token: str) -> None:
        async with self.sessions.begin() as session:
            await conversation_leases.release_lease(session, identity, token=token)
        self._lease_context.release(identity, token)

    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None:
        if not messages:
            return
        async with self.sessions.begin() as session:
            seq = await conversation_leases.reserve_sequence(
                session, identity, len(messages), self._lease_context.token(identity)
            )
            for offset, message in enumerate(messages):
                session.add(_message_row(identity, message, seq + offset))

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

    async def recent_complete_messages(
        self, identity: ConversationIdentity, *, limit: int = 3
    ) -> tuple[ConversationMessage, ...]:
        _require_positive(limit)
        statement = (
            sa.select(ConversationMessageRow)
            .where(
                *_identity_filter(identity),
                ConversationMessageRow.role.in_(("user", "assistant")),
            )
            .order_by(*_NEWEST_FIRST)
            .execution_options(yield_per=100)
        )
        pairs: list[tuple[ConversationMessage, ConversationMessage]] = []
        answer: ConversationMessage | None = None
        async with self.sessions() as session:
            result = await session.stream_scalars(statement)
            try:
                async for row in result:
                    message = _row_to_message(row)
                    if is_complete_reply(message):
                        answer = message
                    elif message.role == "user":
                        if answer is not None and message.content.strip() and not message.partial:
                            pairs.append((message, answer))
                        answer = None
                        if len(pairs) == limit:
                            break
            finally:
                await result.close()
        return tuple(message for pair in reversed(pairs) for message in pair)

    async def clear_messages(self, identity: ConversationIdentity) -> None:
        async with self.sessions.begin() as session:
            if not await conversation_leases.lock_for_write(
                session, identity, self._lease_context.token(identity)
            ):
                return
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
