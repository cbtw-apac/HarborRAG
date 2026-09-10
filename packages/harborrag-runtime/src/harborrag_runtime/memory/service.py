"""Database-backed conversation history plugin owned by the runtime.

The in-memory implementation lives in ``in_memory.py``; it is re-exported here
for callers that imported it from this module before the split.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.ports.conversation import (
    ConversationHistoryRepository,
    ConversationIdentity,
    ConversationKind,
    ConversationMessage,
    ConversationPage,
    ConversationTurn,
)

from .in_memory import InMemoryConversationMemory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings


@dataclass(slots=True)
class DatabaseConversationMemory:
    """Runtime-owned SQL memory plugin; production DSNs use PostgreSQL/asyncpg."""

    repository: ConversationHistoryRepository
    engine: AsyncEngine

    @classmethod
    def configured(
        cls,
        settings: RuntimeSettings | None = None,
    ) -> DatabaseConversationMemory:
        """Compatibility constructor; prefer the package composition factory."""

        from .composition import build_database_conversation_memory

        return build_database_conversation_memory(settings)

    async def recent(
        self,
        identity: ConversationIdentity,
        *,
        limit: int = 2,
    ) -> tuple[ConversationTurn, ...]:
        return await self.repository.recent(identity, limit=limit)

    async def create(
        self,
        identity: ConversationIdentity,
        *,
        kind: ConversationKind = "chat",
        title: str | None = None,
    ) -> None:
        await self.repository.create(identity, kind=kind, title=title)

    async def exists(
        self,
        identity: ConversationIdentity,
        *,
        kind: ConversationKind | None = None,
    ) -> bool:
        return await self.repository.exists(identity, kind=kind)

    async def append(
        self,
        identity: ConversationIdentity,
        turn: ConversationTurn,
    ) -> None:
        await self.repository.append(identity, turn)

    async def delete(self, identity: ConversationIdentity) -> bool:
        return await self.repository.delete(identity)

    async def clear(self, identity: ConversationIdentity) -> None:
        await self.repository.clear(identity)

    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None:
        await self.repository.append_messages(identity, messages)

    async def recent_messages(
        self,
        identity: ConversationIdentity,
        *,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        return await self.repository.recent_messages(identity, limit=limit)

    async def messages_after(
        self,
        identity: ConversationIdentity,
        *,
        after_message_id: str | None,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        return await self.repository.messages_after(
            identity, after_message_id=after_message_id, limit=limit
        )

    async def clear_messages(self, identity: ConversationIdentity) -> None:
        await self.repository.clear_messages(identity)

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        kind: ConversationKind | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ConversationPage:
        return await self.repository.list_conversations(
            tenant_id=tenant_id,
            user_id=user_id,
            kind=kind,
            cursor=cursor,
            limit=limit,
        )

    async def rename_conversation(
        self,
        identity: ConversationIdentity,
        *,
        title: str,
    ) -> bool:
        return await self.repository.rename_conversation(identity, title=title)

    async def aclose(self) -> None:
        await self.engine.dispose()


__all__ = ["DatabaseConversationMemory", "InMemoryConversationMemory"]
