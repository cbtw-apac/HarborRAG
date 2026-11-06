"""Tests for persisted conversation-session creation and isolation."""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control.memory import ConversationSessionService
from harborrag_runtime.memory import InMemoryConversationMemory


@pytest.mark.asyncio
async def test_session_creation_shares_default_user_but_preserves_explicit_scope() -> None:
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
    assert await sessions.exists(
        session_id,
        tenant_id="ACME",
        principal_id="reader-2",
    )
    assert not await sessions.exists(
        session_id, tenant_id="ACME", principal_id="reader-1", user_id="another-user"
    )
    assert not await sessions.exists(session_id, tenant_id="OTHER", principal_id="reader-1")


@pytest.mark.asyncio
async def test_a_session_can_be_named_at_creation() -> None:
    """A client may name the conversation instead of creating then renaming."""

    memory = InMemoryConversationMemory()
    sessions = ConversationSessionService(memory)

    created = await sessions.create(
        tenant_id="ACME",
        principal_id="reader-1",
        user_id="alice",
        title="  Runbook triage  ",
    )
    untitled = await sessions.create(tenant_id="ACME", principal_id="reader-1", user_id="alice")

    page = await memory.list_conversations(tenant_id="ACME", user_id="alice")
    titles = {row.session_id: row.title for row in page.conversations}
    assert titles[str(created.data["session_id"])] == "Runbook triage"
    assert titles[str(untitled.data["session_id"])] is None
