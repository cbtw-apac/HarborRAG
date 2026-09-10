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


@pytest.mark.asyncio
async def test_sessions_are_bound_to_the_surface_that_created_them() -> None:
    sessions = ConversationSessionService(InMemoryConversationMemory())

    chat = str(
        (await sessions.create(tenant_id="ACME", principal_id="r", kind="chat")).data["session_id"]
    )
    agent = str(
        (await sessions.create(tenant_id="ACME", principal_id="r", kind="agent")).data["session_id"]
    )

    assert await sessions.exists(chat, tenant_id="ACME", principal_id="r", kind="chat")
    assert not await sessions.exists(chat, tenant_id="ACME", principal_id="r", kind="agent")
    assert await sessions.exists(agent, tenant_id="ACME", principal_id="r", kind="agent")
    assert not await sessions.exists(agent, tenant_id="ACME", principal_id="r", kind="chat")
    # Without a kind, existence is surface-agnostic.
    assert await sessions.exists(agent, tenant_id="ACME", principal_id="r")


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
