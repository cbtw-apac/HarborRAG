"""Scope-aware short-term, working, and long-term memory for HarborRAG."""

from __future__ import annotations

from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMemory,
    ConversationRepository,
    ConversationSessions,
    ConversationTurn,
    new_session_id,
)

from .config import MemoryManagerConfig
from .errors import MemoryConfigurationError, MemoryError, MemoryScopeError
from .manager import MemoryManager, MemorySnapshot
from .schemas import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    MemoryType,
    new_memory_id,
    scope_owner_fields,
    visible_to,
)
from .tiers import (
    InMemoryWorkingMemoryStore,
    LongTermMemory,
    ShortTermMemory,
    WorkingMemory,
    WorkingMemoryStore,
)

__all__ = [
    "ConversationIdentity",
    "ConversationMemory",
    "ConversationTurn",
    "InMemoryWorkingMemoryStore",
    "LongTermMemory",
    "ConversationRepository",
    "ConversationSessions",
    "Memory",
    "MemoryConfigurationError",
    "MemoryError",
    "MemoryManager",
    "MemoryManagerConfig",
    "MemoryOwner",
    "MemoryQuery",
    "MemoryRepository",
    "MemoryScope",
    "MemoryScopeError",
    "MemorySnapshot",
    "MemoryType",
    "ShortTermMemory",
    "WorkingMemory",
    "WorkingMemoryStore",
    "new_memory_id",
    "new_session_id",
    "scope_owner_fields",
    "visible_to",
]
