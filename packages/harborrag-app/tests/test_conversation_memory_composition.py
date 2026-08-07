"""Unit tests for conversation_memory() composition and its fallback logging."""

from __future__ import annotations

from types import SimpleNamespace

from harborrag_app.workflow_control.memory import conversation_memory
from harborrag_runtime.memory import InMemoryConversationMemory


def test_conversation_memory_uses_the_persistent_control_plane_backend() -> None:
    persistent = object()
    composition = SimpleNamespace(control_plane=SimpleNamespace(conversation_memory=persistent))

    assert conversation_memory(composition) is persistent


def test_conversation_memory_falls_back_to_in_memory_and_warns(caplog) -> None:
    composition = SimpleNamespace(control_plane=SimpleNamespace(conversation_memory=None))

    with caplog.at_level("WARNING", logger="harborrag.app.workflow_control.memory"):
        memory = conversation_memory(composition)

    assert isinstance(memory, InMemoryConversationMemory)
    assert any("falling back to InMemoryConversationMemory" in record.message for record in caplog.records)


def test_conversation_memory_falls_back_when_control_plane_is_missing(caplog) -> None:
    composition = SimpleNamespace()

    with caplog.at_level("WARNING", logger="harborrag.app.workflow_control.memory"):
        memory = conversation_memory(composition)

    assert isinstance(memory, InMemoryConversationMemory)
    assert caplog.records
