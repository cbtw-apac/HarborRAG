"""Bounded process-local conversation history for unit tests and local checks."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ports.completion_requests import STALE_CLAIM_SECONDS, CompletionClaim
from harborrag_core.ports.conversation import (
    CONVERSATION_CURSOR_ERROR,
    MAX_CONVERSATION_PAGE_LIMIT,
    ConversationIdentity,
    ConversationKind,
    ConversationMessage,
    ConversationPage,
    ConversationSummaryRow,
    ConversationTurn,
    complete_message_pairs,
    decode_conversation_cursor,
    encode_conversation_cursor,
    new_message_id,
    normalize_conversation_title,
    turns_from_messages,
)
from harborrag_core.ports.conversation_leases import ConversationLeaseContext


def _require_positive(limit: int) -> None:
    if limit < 1:
        raise ValueError("conversation memory limit must be positive")


@dataclass(slots=True)
class _SessionState:
    """Session metadata the message list itself cannot carry."""

    kind: ConversationKind
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    title_source: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None


class InMemoryConversationMemory:
    """Implements ``ConversationHistoryRepository`` over per-session message lists.

    Sessions are evicted least-recently-used past ``max_sessions``; each
    session keeps its newest ``max_messages`` messages (default
    ``2 * max_turns``, i.e. ``max_turns`` complete user/assistant pairs).
    """

    def __init__(
        self,
        *,
        max_sessions: int = 10_000,
        max_turns: int = 20,
        max_messages: int | None = None,
    ) -> None:
        if max_sessions < 1 or max_turns < 1 or (max_messages is not None and max_messages < 1):
            raise ValueError("conversation memory bounds must be positive")
        self._max_sessions = max_sessions
        self._max_messages = max_messages if max_messages is not None else max_turns * 2
        self._sessions: OrderedDict[
            ConversationIdentity,
            tuple[ConversationMessage, ...],
        ] = OrderedDict()
        self._state: dict[ConversationIdentity, _SessionState] = {}
        self._lock = asyncio.Lock()
        self._completion_claims: dict[tuple[str, str, str], tuple[str, CompletionClaim]] = {}
        self._completion_claimed_at: dict[tuple[str, str, str], datetime] = {}
        self._completion_sessions: dict[tuple[str, str, str], str] = {}
        self._lease_context = ConversationLeaseContext()

    async def claim_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim:
        identity = (tenant_id, user_id, key)
        async with self._lock:
            existing = self._completion_claims.get(identity)
            if existing is not None:
                stored_hash, claim = existing
                if stored_hash != request_hash:
                    return CompletionClaim("conflict")
                # Mirrors SqlCompletionRequestStore: a claim whose holder never
                # came back must not wedge its key, and nothing else ends
                # ``in_progress``.
                claimed_at = self._completion_claimed_at.get(identity)
                abandoned = claimed_at is None or utc_now() - claimed_at >= timedelta(
                    seconds=STALE_CLAIM_SECONDS
                )
                if claim.status != "in_progress" or not abandoned:
                    return claim
            self._completion_claims[identity] = (request_hash, CompletionClaim("in_progress"))
            self._completion_claimed_at[identity] = utc_now()
            return CompletionClaim("claimed")

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
        identity = (tenant_id, user_id, key)
        async with self._lock:
            existing = self._completion_claims.get(identity)
            if (
                existing is None
                or existing[0] != request_hash
                or existing[1].status != "in_progress"
            ):
                return
            if session_id is not None:
                self._completion_sessions[identity] = session_id
                if ConversationIdentity(tenant_id, "", session_id, user_id) not in self._sessions:
                    response_json = None
            self._completion_claims[identity] = (
                request_hash,
                CompletionClaim(
                    "completed" if response_json is not None else "failed", response_json
                ),
            )

    async def release_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> None:
        identity = (tenant_id, user_id, key)
        async with self._lock:
            existing = self._completion_claims.get(identity)
            if (
                existing is None
                or existing[0] != request_hash
                or existing[1].status != "in_progress"
            ):
                return
            del self._completion_claims[identity]
            self._completion_claimed_at.pop(identity, None)
            self._completion_sessions.pop(identity, None)

    # -- sessions ------------------------------------------------------------

    async def create(
        self,
        identity: ConversationIdentity,
        *,
        kind: ConversationKind = "chat",
        title: str | None = None,
    ) -> None:
        now = utc_now()
        async with self._lock:
            self._sessions.setdefault(identity, ())
            self._state.setdefault(
                identity,
                _SessionState(
                    kind=kind,
                    created_at=now,
                    updated_at=now,
                    title=normalize_conversation_title(title),
                    title_source="manual" if normalize_conversation_title(title) else None,
                ),
            )
            self._sessions.move_to_end(identity)
            self._evict()

    async def exists(
        self,
        identity: ConversationIdentity,
        *,
        kind: ConversationKind | None = None,
    ) -> bool:
        async with self._lock:
            if identity not in self._sessions:
                return False
            state = self._state.get(identity)
            return kind is None or (state is not None and state.kind == kind)

    async def delete(self, identity: ConversationIdentity) -> bool:
        """Drop the caller's own session and its messages together."""

        async with self._lock:
            self._require_write_access(identity)
            existed = self._sessions.pop(identity, None) is not None
            self._state.pop(identity, None)
            for key, session_id in self._completion_sessions.items():
                if (
                    key[:2] == (identity.tenant_id, identity.user_id)
                    and session_id == identity.session_id
                ):
                    request_hash, _ = self._completion_claims[key]
                    self._completion_claims[key] = (request_hash, CompletionClaim("failed"))
            return existed

    def _evict(self) -> None:
        while len(self._sessions) > self._max_sessions:
            evicted, _ = self._sessions.popitem(last=False)
            self._state.pop(evicted, None)

    # -- conversation directory ----------------------------------------------

    def _summaries(
        self,
        tenant_id: str,
        user_id: str,
        kind: ConversationKind | None,
    ) -> list[ConversationSummaryRow]:
        rows = [
            ConversationSummaryRow(
                session_id=identity.session_id,
                kind=state.kind,
                title=state.title,
                created_at=state.created_at,
                updated_at=state.updated_at,
                message_count=len(self._sessions.get(identity, ())),
            )
            for identity, state in self._state.items()
            if identity.tenant_id == tenant_id
            and identity.user_id == user_id
            and (kind is None or state.kind == kind)
        ]
        rows.sort(key=lambda row: (row.updated_at, row.session_id), reverse=True)
        return rows

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        kind: ConversationKind | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ConversationPage:
        size = max(1, min(limit, MAX_CONVERSATION_PAGE_LIMIT))
        async with self._lock:
            rows = self._summaries(tenant_id, user_id, kind)
        if cursor is not None:
            anchor = decode_conversation_cursor(cursor)
            positions = [i for i, row in enumerate(rows) if row.session_id == anchor]
            if not positions:
                raise ValueError(CONVERSATION_CURSOR_ERROR)
            rows = rows[positions[0] + 1 :]
        page = tuple(rows[:size])
        has_more = len(rows) > size and bool(page)
        next_cursor = encode_conversation_cursor(page[-1].session_id) if has_more else None
        return ConversationPage(conversations=page, next_cursor=next_cursor)

    async def rename_conversation(
        self,
        identity: ConversationIdentity,
        *,
        title: str,
    ) -> bool:
        async with self._lock:
            state = self._state.get(identity)
            if state is None:
                return False
            state.title = normalize_conversation_title(title)
            state.title_source = "manual"
            return True

    async def set_generated_title(self, identity: ConversationIdentity, *, title: str) -> bool:
        normalized = normalize_conversation_title(title)
        async with self._lock:
            self._require_write_access(identity)
            state = self._state.get(identity)
            if state is None or state.title_source is not None or normalized is None:
                return False
            state.title = normalized
            state.title_source = "generated"
            return True

    async def get_title(self, identity: ConversationIdentity) -> str | None:
        async with self._lock:
            state = self._state.get(identity)
            return state.title if state else None

    async def acquire_turn_lease(
        self, identity: ConversationIdentity, *, token: str, lease_seconds: float
    ) -> bool:
        async with self._lock:
            state = self._state.get(identity)
            now = utc_now()
            if state is None or (
                state.lease_token is not None
                and state.lease_expires_at is not None
                and state.lease_expires_at > now
            ):
                return False
            state.lease_token = token
            state.lease_expires_at = now + timedelta(seconds=lease_seconds)
            self._lease_context.bind(identity, token)
            return True

    async def renew_turn_lease(
        self, identity: ConversationIdentity, *, token: str, lease_seconds: float
    ) -> bool:
        async with self._lock:
            state = self._state.get(identity)
            now = utc_now()
            if (
                state is None
                or state.lease_token != token
                or state.lease_expires_at is None
                or state.lease_expires_at <= now
            ):
                return False
            state.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return True

    async def release_turn_lease(self, identity: ConversationIdentity, *, token: str) -> None:
        async with self._lock:
            state = self._state.get(identity)
            if state is not None and state.lease_token == token:
                state.lease_token = None
                state.lease_expires_at = None
            self._lease_context.release(identity, token)

    def _require_write_access(self, identity: ConversationIdentity) -> None:
        state = self._state.get(identity)
        if state is None:
            return
        token = self._lease_context.token(identity)
        active = state.lease_expires_at is not None and state.lease_expires_at > utc_now()
        if (token is not None and (not active or token != state.lease_token)) or (
            token is None and state.lease_token is not None and active
        ):
            raise HarborConflictError("conversation has an active completion or its lease was lost")

    def _touch(self, identity: ConversationIdentity) -> tuple[ConversationMessage, ...]:
        messages = self._sessions.get(identity, ())
        if messages:
            self._sessions.move_to_end(identity)
        return messages

    # -- per-message history -------------------------------------------------

    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None:
        async with self._lock:
            self._require_write_access(identity)
            if identity not in self._sessions:
                raise ValueError("conversation session does not exist")
            existing = self._sessions[identity]
            self._sessions[identity] = (*existing, *messages)[-self._max_messages :]
            state = self._state.get(identity)
            if state is not None:
                state.updated_at = utc_now()
            self._sessions.move_to_end(identity)
            self._evict()

    async def recent_messages(
        self,
        identity: ConversationIdentity,
        *,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        _require_positive(limit)
        async with self._lock:
            return self._touch(identity)[-limit:]

    async def messages_after(
        self,
        identity: ConversationIdentity,
        *,
        after_message_id: str | None,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        _require_positive(limit)
        async with self._lock:
            messages = self._touch(identity)
        if after_message_id is None:
            return messages[:limit]
        for index, message in enumerate(messages):
            if message.message_id == after_message_id:
                return messages[index + 1 : index + 1 + limit]
        raise ValueError("unknown conversation message cursor")

    async def clear_messages(self, identity: ConversationIdentity) -> None:
        async with self._lock:
            self._require_write_access(identity)
            if identity in self._sessions:
                self._sessions[identity] = ()

    async def recent_complete_messages(
        self, identity: ConversationIdentity, *, limit: int = 3
    ) -> tuple[ConversationMessage, ...]:
        _require_positive(limit)
        async with self._lock:
            return complete_message_pairs(self._touch(identity), limit=limit)

    # -- derived completed-turn view -----------------------------------------

    async def recent(
        self,
        identity: ConversationIdentity,
        *,
        limit: int = 2,
    ) -> tuple[ConversationTurn, ...]:
        _require_positive(limit)
        async with self._lock:
            messages = self._touch(identity)
        return turns_from_messages(messages)[-limit:]

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


__all__ = ["InMemoryConversationMemory"]
