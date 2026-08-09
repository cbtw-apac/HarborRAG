"""Conversation-history contracts and runtime memory implementations."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_memory import (
    ConversationIdentity,
    ConversationMemory,
    ConversationRepository,
    ConversationSessions,
    ConversationTurn,
    MemoryManager,
    MemoryManagerConfig,
    new_session_id,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings


class InMemoryConversationMemory:
    """Bounded process-local implementation for unit tests and local checks."""

    def __init__(self, *, max_sessions: int = 10_000, max_turns: int = 20) -> None:
        if max_sessions < 1 or max_turns < 1:
            raise ValueError("conversation memory bounds must be positive")
        self._max_sessions = max_sessions
        self._max_turns = max_turns
        self._sessions: OrderedDict[
            ConversationIdentity,
            tuple[ConversationTurn, ...],
        ] = OrderedDict()
        self._lock = asyncio.Lock()

    async def create(self, identity: ConversationIdentity) -> None:
        async with self._lock:
            self._sessions.setdefault(identity, ())
            self._sessions.move_to_end(identity)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)

    async def exists(self, identity: ConversationIdentity) -> bool:
        async with self._lock:
            return identity in self._sessions

    async def recent(
        self,
        identity: ConversationIdentity,
        *,
        limit: int = 2,
    ) -> tuple[ConversationTurn, ...]:
        if limit < 1:
            raise ValueError("conversation memory limit must be positive")
        async with self._lock:
            turns = self._sessions.get(identity, ())
            if turns:
                self._sessions.move_to_end(identity)
            return turns[-limit:]

    async def append(
        self,
        identity: ConversationIdentity,
        turn: ConversationTurn,
    ) -> None:
        async with self._lock:
            if identity not in self._sessions:
                raise ValueError("conversation session does not exist")
            existing = self._sessions.get(identity, ())
            self._sessions[identity] = (*existing, turn)[-self._max_turns :]
            self._sessions.move_to_end(identity)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)

    async def clear(self, identity: ConversationIdentity) -> None:
        async with self._lock:
            if identity in self._sessions:
                self._sessions[identity] = ()


@dataclass(slots=True)
class DatabaseConversationMemory:
    """Runtime-owned SQL memory plugin; production DSNs use PostgreSQL/asyncpg."""

    repository: ConversationRepository
    engine: AsyncEngine

    @classmethod
    def configured(
        cls,
        settings: RuntimeSettings | None = None,
    ) -> DatabaseConversationMemory:
        """Migrate and open the configured control DB for a standalone process."""

        from harborrag_adapters.repositories.database.control_plane.conversation import (
            SqlConversationMemoryRepository,
        )
        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
            create_session_factory,
        )
        from harborrag_adapters.repositories.database.control_plane.migrations import (
            run_migrations,
        )
        from harborrag_core.contracts.errors import HarborConfigurationError
        from harborrag_runtime.config.settings import DEFAULT_CONTROL_DB_URL, RuntimeSettings

        selected = settings or RuntimeSettings()
        dsn = selected.control_db_url.get_secret_value()
        if selected.env == "prod" and dsn == DEFAULT_CONTROL_DB_URL:
            raise HarborConfigurationError(
                "conversation memory requires HARBORRAG_CONTROL_DB_URL in production"
            )
        run_migrations(dsn)
        engine = create_control_plane_engine(dsn)
        repository = SqlConversationMemoryRepository(create_session_factory(engine))
        return cls(repository=repository, engine=engine)

    async def recent(
        self,
        identity: ConversationIdentity,
        *,
        limit: int = 2,
    ) -> tuple[ConversationTurn, ...]:
        return await self.repository.recent(identity, limit=limit)

    async def create(self, identity: ConversationIdentity) -> None:
        await self.repository.create(identity)

    async def exists(self, identity: ConversationIdentity) -> bool:
        return await self.repository.exists(identity)

    async def append(
        self,
        identity: ConversationIdentity,
        turn: ConversationTurn,
    ) -> None:
        await self.repository.append(identity, turn)

    async def clear(self, identity: ConversationIdentity) -> None:
        await self.repository.clear(identity)

    async def aclose(self) -> None:
        await self.engine.dispose()


__all__ = [
    "ConversationIdentity",
    "ConversationMemory",
    "ConversationRepository",
    "ConversationSessions",
    "ConversationTurn",
    "DatabaseConversationMemory",
    "InMemoryConversationMemory",
    "MemoryManager",
    "MemoryManagerConfig",
    "new_session_id",
]
