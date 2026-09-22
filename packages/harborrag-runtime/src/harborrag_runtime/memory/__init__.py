"""Conversation-history contracts and runtime memory implementations."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from harborrag_core.ports.conversation import (
    MAX_CONVERSATION_PAGE_LIMIT,
    MAX_CONVERSATION_TITLE_LENGTH,
    ConversationDirectory,
    ConversationHistoryRepository,
    ConversationKind,
    ConversationMessage,
    ConversationMessageStore,
    ConversationPage,
    ConversationRole,
    ConversationSummaryRow,
    decode_conversation_cursor,
    encode_conversation_cursor,
    new_message_id,
    normalize_conversation_title,
    turns_from_messages,
)
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    MemoryType,
)
from harborrag_core.ports.usage import (
    ModelUsageRecord,
    ModelUsageRepository,
    ModelUsageTotals,
    UsageSurface,
    new_usage_id,
)
from harborrag_runtime.contracts import MemoryContextRequest

if TYPE_CHECKING:
    from harborrag_memory import (
        ConversationIdentity,
        ConversationMemory,
        ConversationRepository,
        ConversationSessions,
        ConversationTurn,
        MemoryContext,
        MemoryContextBuilder,
        MemoryManager,
        MemoryManagerConfig,
        MemoryPolicy,
        new_session_id,
        recalled_entity_ids,
    )

    from .composition import build_database_conversation_memory
    from .context_service import RuntimeMemoryContextService
    from .embedder import build_memory_embedder, close_memory_embedder
    from .entities import GraphMemoryEntityResolver, build_memory_entity_resolver
    from .facade import MemoryFacade
    from .in_memory import InMemoryConversationMemory
    from .index import build_memory_index
    from .model import build_memory_chat_model, close_memory_chat_model
    from .policy import memory_policy_from_settings
    from .service import DatabaseConversationMemory

_MEMORY_EXPORTS = frozenset(
    {
        "ConversationIdentity",
        "ConversationMemory",
        "ConversationRepository",
        "ConversationSessions",
        "ConversationTurn",
        "MemoryContext",
        "MemoryContextBuilder",
        "MemoryManager",
        "MemoryManagerConfig",
        "MemoryPolicy",
        "new_session_id",
        "recalled_entity_ids",
    }
)
_RUNTIME_EXPORTS = {
    "build_database_conversation_memory": ".composition",
    "RuntimeMemoryContextService": ".context_service",
    "build_memory_embedder": ".embedder",
    "close_memory_embedder": ".embedder",
    "GraphMemoryEntityResolver": ".entities",
    "build_memory_entity_resolver": ".entities",
    "MemoryFacade": ".facade",
    "InMemoryConversationMemory": ".in_memory",
    "build_memory_index": ".index",
    "build_memory_chat_model": ".model",
    "close_memory_chat_model": ".model",
    "memory_policy_from_settings": ".policy",
    "DatabaseConversationMemory": ".service",
}

__all__ = [
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
    "DatabaseConversationMemory",
    "GraphMemoryEntityResolver",
    "InMemoryConversationMemory",
    "Memory",
    "MemoryContext",
    "MemoryContextBuilder",
    "MemoryContextRequest",
    "MemoryFacade",
    "MemoryManager",
    "MemoryManagerConfig",
    "MemoryOwner",
    "MemoryPolicy",
    "MemoryQuery",
    "MemoryRepository",
    "MemoryScope",
    "MemoryType",
    "ModelUsageRecord",
    "ModelUsageRepository",
    "ModelUsageTotals",
    "RuntimeMemoryContextService",
    "UsageSurface",
    "build_memory_chat_model",
    "build_memory_embedder",
    "build_memory_index",
    "build_memory_entity_resolver",
    "close_memory_chat_model",
    "close_memory_embedder",
    "memory_policy_from_settings",
    "decode_conversation_cursor",
    "encode_conversation_cursor",
    "recalled_entity_ids",
    "new_message_id",
    "new_session_id",
    "new_usage_id",
    "normalize_conversation_title",
    "build_database_conversation_memory",
    "turns_from_messages",
]


def __getattr__(name: str) -> Any:
    """Load optional memory behavior only when a caller requests it."""
    if name in _MEMORY_EXPORTS:
        return getattr(import_module("harborrag_memory"), name)
    module_name = _RUNTIME_EXPORTS.get(name)
    if module_name is not None:
        return getattr(import_module(module_name, __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
