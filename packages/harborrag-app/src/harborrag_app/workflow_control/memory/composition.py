"""Resolve the configured conversation-memory plugin for the application."""

from __future__ import annotations

from typing import Any

from harborrag_runtime.memory import ConversationRepository, InMemoryConversationMemory


def conversation_memory(composition: Any) -> ConversationRepository:
    """Use persistent composition memory, with a test-double fallback."""

    control_plane = getattr(composition, "control_plane", None)
    persistent = getattr(control_plane, "conversation_memory", None)
    return persistent if persistent is not None else InMemoryConversationMemory()


__all__ = ["conversation_memory"]
