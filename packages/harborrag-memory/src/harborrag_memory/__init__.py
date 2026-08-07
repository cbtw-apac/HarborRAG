"""Scope-aware short-term, working, and long-term memory for HarborRAG."""

from __future__ import annotations

from harborrag_core.ports.conversation import ConversationRepository, ConversationSessions, new_session_id

from .config import MemoryManagerConfig
from .long_term.memory import LongTermMemory
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
from .short_term.memory import (
	ConversationIdentity,
	ConversationMemory,
	ConversationTurn,
	ShortTermMemory,
)
from .working.memory import InMemoryWorkingMemoryStore, WorkingMemory, WorkingMemoryStore

__all__ = [
	"ConversationIdentity",
	"ConversationMemory",
	"ConversationTurn",
	"InMemoryWorkingMemoryStore",
	"LongTermMemory",
	"ConversationRepository",
	"ConversationSessions",
	"Memory",
	"MemoryManager",
	"MemoryManagerConfig",
	"MemoryOwner",
	"MemoryQuery",
	"MemoryRepository",
	"MemoryScope",
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
