"""Resolve the configured conversation-memory and agent-run-checkpoint plugins."""

from __future__ import annotations

import logging
from typing import Any

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_runtime.agent import AgentRunRepository, InMemoryAgentRunRepository
from harborrag_runtime.memory import ConversationHistoryRepository, InMemoryConversationMemory

logger = logging.getLogger("harborrag.app.workflow_control.memory")
_IN_MEMORY_MODES = frozenset({"development", "test"})


def _require_in_memory_mode(composition: Any, repository_name: str) -> None:
    mode = getattr(composition, "mode", None)
    if mode not in _IN_MEMORY_MODES:
        raise HarborConfigurationError(
            f"persistent {repository_name} is required in composition mode {mode!r}"
        )


def conversation_memory(composition: Any) -> ConversationHistoryRepository:
    """Use persistent composition memory, with a test-double fallback."""

    control_plane = getattr(composition, "control_plane", None)
    persistent: ConversationHistoryRepository | None = getattr(
        control_plane, "conversation_memory", None
    )
    if persistent is not None:
        return persistent
    _require_in_memory_mode(composition, "conversation_memory")
    logger.warning(
        "No persistent conversation_memory wired on the control plane; falling back to "
        "InMemoryConversationMemory. Chat and agent sessions will not survive a restart "
        "and will not be shared across replicas."
    )
    return InMemoryConversationMemory()


def long_term_memories(composition: Any) -> MemoryRepository | None:
    """The durable memory store, or None when none is wired.

    Unlike conversation history, this one is optional: without it the memory
    layer still trims and replays the recent window, it just cannot persist
    rolling summaries or recall older facts. That degrades an enhancement
    rather than breaking chat, so a missing store warns instead of raising.
    """

    control_plane = getattr(composition, "control_plane", None)
    repository: MemoryRepository | None = getattr(control_plane, "memories", None)
    if repository is None and getattr(composition, "mode", None) not in _IN_MEMORY_MODES:
        logger.warning(
            "No long-term memory repository wired on the control plane; conversation "
            "summaries and long-term recall are disabled for this process."
        )
    return repository


def long_term_memory_index(composition: Any) -> MemoryIndex | None:
    """The memory vector index, or None when this deployment wires none.

    Optional like the memory store itself: without it recall and extraction
    dedup fall back to the repository's lexical filter, which is a weaker
    ranking rather than a broken turn.
    """

    control_plane = getattr(composition, "control_plane", None)
    index: MemoryIndex | None = getattr(control_plane, "memory_index", None)
    if index is None:
        index = getattr(composition, "memory_index", None)
    return index


def model_usage_records(composition: Any) -> ModelUsageRepository | None:
    """The durable per-request usage ledger, or None when none is wired.

    Optional like the long-term memory store: without it a turn still answers,
    it just leaves no accounting trail. That is a real gap outside
    development, so a missing ledger warns rather than falling back to a
    store nobody could bill from.
    """

    control_plane = getattr(composition, "control_plane", None)
    repository: ModelUsageRepository | None = getattr(control_plane, "model_usage", None)
    if repository is None and getattr(composition, "mode", None) not in _IN_MEMORY_MODES:
        logger.warning(
            "No model_usage repository wired on the control plane; chat and agent token "
            "spend will not be recorded per tenant and user for this process."
        )
    return repository


def agent_run_checkpoints(composition: Any) -> AgentRunRepository:
    """Use persistent composition agent-run checkpoints, with a test-double fallback."""

    control_plane = getattr(composition, "control_plane", None)
    persistent: AgentRunRepository | None = getattr(control_plane, "agent_runs", None)
    if persistent is not None:
        return persistent
    _require_in_memory_mode(composition, "agent_runs")
    logger.warning(
        "No persistent agent_runs repository wired on the control plane; falling back to "
        "InMemoryAgentRunRepository. Agent runs will not survive a restart and will not be "
        "shared across replicas."
    )
    return InMemoryAgentRunRepository()


__all__ = [
    "agent_run_checkpoints",
    "conversation_memory",
    "long_term_memories",
    "long_term_memory_index",
    "model_usage_records",
]
