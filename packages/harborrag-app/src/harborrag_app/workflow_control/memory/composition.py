"""Resolve the configured conversation-memory plugin for the application."""

from __future__ import annotations

import logging
from typing import Any

from harborrag_runtime.memory import ConversationRepository, InMemoryConversationMemory

logger = logging.getLogger("harborrag.app.workflow_control.memory")


def conversation_memory(composition: Any) -> ConversationRepository:
    """Use persistent composition memory, with a test-double fallback."""

    control_plane = getattr(composition, "control_plane", None)
    persistent: ConversationRepository | None = getattr(
        control_plane, "conversation_memory", None
    )
    if persistent is not None:
        return persistent
    logger.warning(
        "No persistent conversation_memory wired on the control plane; falling back to "
        "InMemoryConversationMemory. Chat and agent sessions will not survive a restart "
        "and will not be shared across replicas."
    )
    return InMemoryConversationMemory()


__all__ = ["conversation_memory"]
