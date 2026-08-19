"""Conversation-history contracts and runtime memory implementations."""

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

from .composition import build_database_conversation_memory
from .service import DatabaseConversationMemory, InMemoryConversationMemory

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
    "build_database_conversation_memory",
]
