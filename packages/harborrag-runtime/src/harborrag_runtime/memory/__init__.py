"""Conversation-history contracts and runtime memory implementations."""

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
from harborrag_runtime.contracts import MemoryContextRequest

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
