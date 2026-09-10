"""Conversation-memory port shared by chat, agents, and persistence adapters.

Two views of the same history coexist here. ``ConversationMessage`` is the
canonical per-message record (user, assistant, tool, and system messages with
their tool-call and citation payloads); ``ConversationTurn`` is the legacy
completed user/assistant pair that chat and agent prompt builders still
consume. Adapters persist messages only and derive turns from them with
``turns_from_messages`` so both views can never disagree.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import uuid4

ConversationKind = Literal["chat", "agent"]
"""Which completion surface owns a session.

Chat and agent turns share one memory table, so a session is bound to the
surface that created it: a chat completion on an agent session (or the
reverse) is treated as an unknown session rather than interleaving turns.
"""

ConversationRole = Literal["user", "assistant", "tool", "system"]
"""Who authored one persisted conversation message."""

MAX_CONVERSATION_TITLE_LENGTH = 200
"""Longest stored conversation title; longer titles are truncated, not rejected."""

MAX_CONVERSATION_PAGE_LIMIT = 100
"""Largest page a conversation listing will return, whatever the caller asks for."""


def normalize_conversation_title(title: str | None) -> str | None:
    """Trim a caller-supplied title and bound it; blank becomes ``None``."""

    if title is None:
        return None
    trimmed = title.strip()
    if not trimmed:
        return None
    return trimmed[:MAX_CONVERSATION_TITLE_LENGTH]


CONVERSATION_CURSOR_ERROR = "unknown conversation cursor"
"""Message every conversation-listing cursor rejection carries."""


def encode_conversation_cursor(session_id: str) -> str:
    """Encode a session id as the opaque cursor of a conversation listing."""

    return base64.urlsafe_b64encode(session_id.encode()).decode().rstrip("=")


def decode_conversation_cursor(cursor: str) -> str:
    """Decode a listing cursor; malformed input raises ``ValueError``.

    Every implementation shares this codec so an unknown or tampered cursor
    fails the same way it does for the ``messages_after`` message-id cursor,
    rather than silently paging from the start of the listing.
    """

    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(CONVERSATION_CURSOR_ERROR) from exc
    if not decoded.strip():
        raise ValueError(CONVERSATION_CURSOR_ERROR)
    return decoded


def new_session_id() -> str:
    """Generate one API-safe opaque conversation session identifier."""

    return f"session-{uuid4().hex}"


def new_message_id() -> str:
    """Generate one API-safe opaque conversation message identifier."""

    return f"msg-{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    """Isolation key for one authenticated conversation session.

    ``user_id`` is the human who owns the conversation and is load-bearing
    for every predicate; ``principal_id`` is only the credential that acted,
    kept for audit and provenance. One service principal fronting several
    people must not make ``session_id`` the sole separator.
    """

    tenant_id: str
    principal_id: str
    session_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One completed user/assistant exchange persisted as conversation memory."""

    user_content: str
    assistant_content: str


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """One persisted message of a conversation, in the order it was appended.

    ``message_id`` is caller-generated (see ``new_message_id``) so a caller can
    reference the message it just wrote -- as a paging cursor for
    ``messages_after`` or as memory provenance -- without a read-back.
    """

    message_id: str
    role: ConversationRole
    content: str
    created_at: datetime
    token_count: int | None = None
    tool_calls_json: str | None = None
    tool_call_id: str | None = None
    citations_json: str | None = None
    run_id: str | None = None
    partial: bool = False
    """True when the stored text is only what a stream delivered before it ended.

    A deadline, a provider error, or a client disconnect can end a turn after
    some text has reached the user. That text is persisted rather than lost,
    but a reader must be able to tell it apart from a finished answer, so the
    flag is part of the canonical record and not a transport detail.
    """

    def __post_init__(self) -> None:
        if not self.message_id.strip():
            raise ValueError("conversation message id must be non-empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("conversation message created_at must be timezone-aware")
        if self.token_count is not None and self.token_count < 0:
            raise ValueError("conversation message token_count must not be negative")


def turns_from_messages(messages: Iterable[ConversationMessage]) -> tuple[ConversationTurn, ...]:
    """Derive completed user/assistant turns from oldest-first messages.

    A turn is a user message followed by the next assistant message. Tool and
    system messages are skipped; a user message with no assistant reply yet
    (or displaced by a later user message) does not form a turn, and an
    assistant message without a preceding user message is ignored.
    """

    turns: list[ConversationTurn] = []
    pending_user: str | None = None
    for message in messages:
        if message.role == "user":
            pending_user = message.content
        elif message.role == "assistant" and pending_user is not None:
            turns.append(ConversationTurn(pending_user, message.content))
            pending_user = None
    return tuple(turns)


class ConversationMemory(Protocol):
    """Persistence-neutral completed-turn memory contract."""

    async def recent(
        self,
        identity: ConversationIdentity,
        *,
        limit: int = 2,
    ) -> tuple[ConversationTurn, ...]: ...

    async def append(
        self,
        identity: ConversationIdentity,
        turn: ConversationTurn,
    ) -> None: ...

    async def clear(self, identity: ConversationIdentity) -> None: ...


class ConversationMessageStore(Protocol):
    """Persistence-neutral per-message history contract."""

    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None:
        """Persist ``messages`` after every existing message, in the given order."""
        ...

    async def recent_messages(
        self,
        identity: ConversationIdentity,
        *,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Return the last ``limit`` messages, oldest-first."""
        ...

    async def messages_after(
        self,
        identity: ConversationIdentity,
        *,
        after_message_id: str | None,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Return up to ``limit`` messages strictly after ``after_message_id``, oldest-first.

        ``None`` pages from the start of the history. An ``after_message_id``
        that does not belong to ``identity`` raises ``ValueError`` so a stale
        cursor is never silently mistaken for the start of the history.
        """
        ...

    async def clear_messages(self, identity: ConversationIdentity) -> None: ...


class ConversationSessions(Protocol):
    """Lifecycle contract for persisted conversation session resources."""

    async def create(
        self,
        identity: ConversationIdentity,
        *,
        kind: ConversationKind = "chat",
        title: str | None = None,
    ) -> None: ...

    async def exists(
        self,
        identity: ConversationIdentity,
        *,
        kind: ConversationKind | None = None,
    ) -> bool:
        """Report whether the session exists; ``kind`` additionally requires a match."""
        ...

    async def delete(self, identity: ConversationIdentity) -> bool:
        """Remove the session itself, reporting whether it was the caller's.

        Clearing a session's messages is not the same as deleting it: an
        emptied session still exists, so it keeps appearing in the caller's
        conversation list. Erasure needs this to actually remove the row.
        """
        ...


@dataclass(frozen=True, slots=True)
class ConversationSummaryRow:
    """One row of a conversation listing, without any message content."""

    session_id: str
    kind: ConversationKind
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """One page of a user's conversations, newest-activity first."""

    conversations: tuple[ConversationSummaryRow, ...]
    next_cursor: str | None


class ConversationDirectory(Protocol):
    """Listing and renaming of the conversations one human owns."""

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        kind: ConversationKind | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ConversationPage:
        """Return the user's conversations newest-activity first.

        ``limit`` is clamped to 1..``MAX_CONVERSATION_PAGE_LIMIT``. ``cursor``
        is opaque and only valid for this ordering; an unknown or malformed
        cursor raises ``ValueError`` rather than silently paging from the start.
        """
        ...

    async def rename_conversation(
        self,
        identity: ConversationIdentity,
        *,
        title: str,
    ) -> bool:
        """Retitle the conversation; ``False`` when it is not the caller's."""
        ...


class ConversationRepository(ConversationMemory, ConversationSessions, Protocol):
    """Combined session lifecycle and completed-turn persistence contract."""


class ConversationHistoryRepository(
    ConversationRepository,
    ConversationMessageStore,
    ConversationDirectory,
    Protocol,
):
    """Session lifecycle, both history views, and the conversation directory."""


__all__ = [
    "CONVERSATION_CURSOR_ERROR",
    "MAX_CONVERSATION_PAGE_LIMIT",
    "MAX_CONVERSATION_TITLE_LENGTH",
    "ConversationDirectory",
    "ConversationHistoryRepository",
    "ConversationIdentity",
    "ConversationKind",
    "ConversationMemory",
    "ConversationMessage",
    "ConversationMessageStore",
    "ConversationPage",
    "ConversationRepository",
    "ConversationRole",
    "ConversationSessions",
    "ConversationSummaryRow",
    "ConversationTurn",
    "decode_conversation_cursor",
    "encode_conversation_cursor",
    "new_message_id",
    "new_session_id",
    "normalize_conversation_title",
    "turns_from_messages",
]
