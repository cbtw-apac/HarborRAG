"""Tests for persisted conversation-session creation and isolation."""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control.memory import ConversationSessionService
from harborrag_runtime.memory import InMemoryConversationMemory


@pytest.mark.asyncio
async def test_session_creation_returns_greeting_and_isolates_owner() -> None:
    sessions = ConversationSessionService(
        InMemoryConversationMemory(),
        greetings=("Hello from HarborRAG",),
    )

    created = await sessions.create(tenant_id="ACME", principal_id="reader-1")
    session_id = str(created.data["session_id"])

    assert created.data == {
        "session_id": session_id,
        "greeting": "Hello from HarborRAG",
    }
    assert await sessions.exists(
        session_id,
        tenant_id="ACME",
        principal_id="reader-1",
    )
    assert not await sessions.exists(
        session_id,
        tenant_id="ACME",
        principal_id="reader-2",
    )
