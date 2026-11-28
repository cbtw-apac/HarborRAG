"""Database-backed conversation history plugin owned by the runtime."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.ports.completion_requests import CompletionClaim
from harborrag_core.ports.conversation import (
    ConversationHistoryRepository,
    ConversationIdentity,
    ConversationMessage,
    ConversationPage,
    ConversationSummaryRow,
    ConversationTurn,
)

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
        """Return the most recent turns of a conversation."""
        return await self.repository.recent(identity, limit=limit)

    async def create(
        self,
        identity: ConversationIdentity,
        *,
        title: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Create a conversation, optionally titled and tied to a project."""
        await self.repository.create(identity, title=title, project_id=project_id)

    async def exists(self, identity: ConversationIdentity) -> bool:
        """Return whether the conversation exists."""
        return await self.repository.exists(identity)

    async def append(
        self,
        identity: ConversationIdentity,
        turn: ConversationTurn,
    ) -> None:
        """Append one turn to the conversation."""
        await self.repository.append(identity, turn)

    async def delete(self, identity: ConversationIdentity) -> bool:
        """Delete the conversation; return whether it existed."""
        return await self.repository.delete(identity)

    async def clear(self, identity: ConversationIdentity) -> None:
        """Remove every turn of the conversation."""
        await self.repository.clear(identity)

    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None:
        """Append messages to the conversation."""
        await self.repository.append_messages(identity, messages)

    async def recent_messages(
        self,
        identity: ConversationIdentity,
        *,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Return the latest ``limit`` messages."""
        return await self.repository.recent_messages(identity, limit=limit)

    async def messages_after(
        self,
        identity: ConversationIdentity,
        *,
        after_message_id: str | None,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Return messages that follow ``after_message_id``, oldest first."""
        return await self.repository.messages_after(
            identity, after_message_id=after_message_id, limit=limit
        )

    async def history_page(
        self,
        identity: ConversationIdentity,
        *,
        before_message_id: str | None,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Return a page of messages that precede ``before_message_id``."""
        return await self.repository.history_page(
            identity, before_message_id=before_message_id, limit=limit
        )

    async def clear_messages(self, identity: ConversationIdentity) -> None:
        """Remove every message of the conversation."""
        await self.repository.clear_messages(identity)

    async def recent_complete_messages(
        self, identity: ConversationIdentity, *, limit: int = 3
    ) -> tuple[ConversationMessage, ...]:
        """Return the latest complete messages."""
        return await self.repository.recent_complete_messages(identity, limit=limit)

    async def list_conversations(  # noqa: PLR0913 - mirrors the directory port
        self,
        *,
        tenant_id: str,
        user_id: str,
        cursor: str | None = None,
        limit: int = 20,
        title_query: str | None = None,
    ) -> ConversationPage:
        """List a user's conversations, one page at a time."""
        return await self.repository.list_conversations(
            tenant_id=tenant_id,
            user_id=user_id,
            cursor=cursor,
            limit=limit,
            title_query=title_query,
        )

    async def get_conversation(
        self, identity: ConversationIdentity
    ) -> ConversationSummaryRow | None:
        """Return the conversation's summary row, or None."""
        return await self.repository.get_conversation(identity)

    async def rename_conversation(
        self,
        identity: ConversationIdentity,
        *,
        title: str,
    ) -> bool:
        """Rename the conversation; return whether it exists."""
        return await self.repository.rename_conversation(identity, title=title)

    async def aclose(self) -> None:
        """Dispose of the database engine."""
        await self.engine.dispose()

    async def claim_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim:
        """Claim a completion for an idempotency key."""
        return await self.repository.claim_completion(
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
        """Store the response of a claimed completion."""
        await self.repository.finish_completion(
            tenant_id=tenant_id,
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            response_json=response_json,
            session_id=session_id,
        )

    async def release_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> None:
        """Release a claimed completion without a response."""
        await self.repository.release_completion(
            tenant_id=tenant_id, user_id=user_id, key=key, request_hash=request_hash
        )

    async def set_generated_title(self, identity: ConversationIdentity, *, title: str) -> bool:
        """Set the generated title; return whether it was applied."""
        return await self.repository.set_generated_title(identity, title=title)

    async def get_title(self, identity: ConversationIdentity) -> str | None:
        """Return the conversation's title, or None."""
        return await self.repository.get_title(identity)

    async def acquire_turn_lease(
        self, identity: ConversationIdentity, *, token: str, lease_seconds: float
    ) -> bool:
        """Take the conversation's turn lease; return whether it was granted."""
        return await self.repository.acquire_turn_lease(
            identity, token=token, lease_seconds=lease_seconds
        )

    async def renew_turn_lease(
        self, identity: ConversationIdentity, *, token: str, lease_seconds: float
    ) -> bool:
        """Extend a held turn lease; return whether it was still held."""
        return await self.repository.renew_turn_lease(
            identity, token=token, lease_seconds=lease_seconds
        )

    async def release_turn_lease(self, identity: ConversationIdentity, *, token: str) -> None:
        """Release a held turn lease."""
        await self.repository.release_turn_lease(identity, token=token)
