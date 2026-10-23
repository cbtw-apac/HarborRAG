"""Conversation-memory contract the agent engine consumes.

The engine reads completed turns (``recent``) to seed a run and writes the
run's user question and final answer as individual messages
(``append_messages``); tool traffic stays in the agent-run checkpoint. Any
repository implementing ``harborrag_core.ports.conversation`` satisfies this
structurally.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMessage,
    ConversationTurn,
)


class ConversationMemory(Protocol):
    """Turn-level reads plus per-message writes for one conversation session."""

    async def recent(
        self,
        identity: ConversationIdentity,
        *,
        limit: int = 2,
    ) -> tuple[ConversationTurn, ...]: ...

    async def recent_messages(
        self,
        identity: ConversationIdentity,
        *,
        limit: int,
    ) -> tuple[ConversationMessage, ...]: ...

    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None: ...


__all__ = ["ConversationIdentity", "ConversationMemory", "ConversationMessage", "ConversationTurn"]
