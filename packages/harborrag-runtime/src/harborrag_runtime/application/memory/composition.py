"""Conversation-memory plugins resolved from the composition's control plane."""

from __future__ import annotations

import logging

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.observability.log_events import log_event
from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory
from harborrag_runtime.memory import ConversationHistoryRepository

logger = logging.getLogger("harborrag.runtime.application.memory.composition")
_IN_MEMORY_MODES = frozenset({"development", "test"})


def _require_in_memory_mode(composition: object, repository_name: str) -> None:
    """Refuse an in-process fallback outside development and test composition modes."""
    mode = getattr(composition, "mode", None)
    if mode not in _IN_MEMORY_MODES:
        raise HarborConfigurationError(
            f"persistent {repository_name} is required in composition mode {mode!r}"
        )


def conversation_memory(composition: object) -> ConversationHistoryRepository:
    """Use persistent composition memory, with a test-double fallback."""
    control_plane = getattr(composition, "control_plane", None)
    persistent: ConversationHistoryRepository | None = getattr(
        control_plane, "conversation_memory", None
    )
    if persistent is not None:
        return persistent
    _require_in_memory_mode(composition, "conversation_memory")
    log_event(
        logger,
        "memory.conversation_memory_in_process",
        level=logging.WARNING,
        hint=(
            "Run with the control-plane database (HARBORRAG_CONTROL_DB_URL); in-process "
            "chat sessions are lost on restart and not shared across replicas."
        ),
    )
    return InMemoryConversationMemory()


def long_term_memories(composition: object) -> MemoryRepository | None:
    """Return the durable memory store, or None when none is wired.

    Unlike conversation history, this one is optional: without it the memory
    layer still trims and replays the recent window, it just cannot persist
    rolling summaries or recall older facts. That degrades an enhancement
    rather than breaking chat, so a missing store warns instead of raising.
    """
    control_plane = getattr(composition, "control_plane", None)
    repository: MemoryRepository | None = getattr(control_plane, "memories", None)
    if repository is None and getattr(composition, "mode", None) not in _IN_MEMORY_MODES:
        log_event(
            logger,
            "memory.long_term_memory_unwired",
            level=logging.WARNING,
            hint=(
                "Run with the control-plane database (HARBORRAG_CONTROL_DB_URL); until then "
                "conversation summaries and long-term recall are off in this process."
            ),
        )
    return repository


def long_term_memory_index(composition: object) -> MemoryIndex | None:
    """Return the memory vector index, or None when this deployment wires none.

    Optional like the memory store itself: without it recall and extraction
    dedup fall back to the repository's lexical filter, which is a weaker
    ranking rather than a broken turn.
    """
    control_plane = getattr(composition, "control_plane", None)
    index: MemoryIndex | None = getattr(control_plane, "memory_index", None)
    if index is None:
        index = getattr(composition, "memory_index", None)
    return index


def model_usage_records(composition: object) -> ModelUsageRepository | None:
    """Return the durable per-request usage ledger, or None when none is wired.

    Optional like the long-term memory store: without it a turn still answers,
    it just leaves no accounting trail. That is a real gap outside
    development, so a missing ledger warns rather than falling back to a
    store nobody could bill from.
    """
    control_plane = getattr(composition, "control_plane", None)
    repository: ModelUsageRepository | None = getattr(control_plane, "model_usage", None)
    if repository is None and getattr(composition, "mode", None) not in _IN_MEMORY_MODES:
        log_event(
            logger,
            "memory.model_usage_unwired",
            level=logging.WARNING,
            hint=(
                "Run with the control-plane database (HARBORRAG_CONTROL_DB_URL); until then "
                "chat token spend is not recorded per tenant and user in this process."
            ),
        )
    return repository
