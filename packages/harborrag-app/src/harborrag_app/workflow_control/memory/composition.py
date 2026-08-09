"""Resolve the configured conversation-memory and agent-run-checkpoint plugins."""

from __future__ import annotations

import logging
from typing import Any

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_runtime.agent import AgentRunRepository, InMemoryAgentRunRepository
from harborrag_runtime.memory import ConversationRepository, InMemoryConversationMemory

logger = logging.getLogger("harborrag.app.workflow_control.memory")
_IN_MEMORY_MODES = frozenset({"development", "test"})


def _require_in_memory_mode(composition: Any, repository_name: str) -> None:
    mode = getattr(composition, "mode", None)
    if mode not in _IN_MEMORY_MODES:
        raise HarborConfigurationError(
            f"persistent {repository_name} is required in composition mode {mode!r}"
        )


def conversation_memory(composition: Any) -> ConversationRepository:
    """Use persistent composition memory, with a test-double fallback."""

    control_plane = getattr(composition, "control_plane", None)
    persistent: ConversationRepository | None = getattr(control_plane, "conversation_memory", None)
    if persistent is not None:
        return persistent
    _require_in_memory_mode(composition, "conversation_memory")
    logger.warning(
        "No persistent conversation_memory wired on the control plane; falling back to "
        "InMemoryConversationMemory. Chat and agent sessions will not survive a restart "
        "and will not be shared across replicas."
    )
    return InMemoryConversationMemory()


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


__all__ = ["agent_run_checkpoints", "conversation_memory"]
