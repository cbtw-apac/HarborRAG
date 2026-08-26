"""Conversation-memory port shared by chat, agents, and persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


def new_session_id() -> str:
    """Generate one API-safe opaque conversation session identifier."""

    return f"session-{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    """Isolation key for one authenticated conversation session."""

    tenant_id: str
    principal_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One completed user/assistant exchange persisted as conversation memory."""

    user_content: str
    assistant_content: str


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


class ConversationSessions(Protocol):
    """Lifecycle contract for persisted conversation session resources."""

    async def create(self, identity: ConversationIdentity) -> None: ...

    async def exists(self, identity: ConversationIdentity) -> bool: ...


class ConversationRepository(ConversationMemory, ConversationSessions, Protocol):
    """Combined session lifecycle and completed-turn persistence contract."""


__all__ = [
    "ConversationIdentity",
    "ConversationMemory",
    "ConversationRepository",
    "ConversationSessions",
    "ConversationTurn",
    "new_session_id",
]
