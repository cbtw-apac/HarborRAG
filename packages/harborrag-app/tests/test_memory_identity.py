"""MemoryIdentity maps the transport's caller onto both memory isolation keys."""

from __future__ import annotations

from harborrag_app.workflow_control.memory import MemoryIdentity
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import MemoryOwner


def test_identity_projects_owner_and_conversation_keys() -> None:
    identity = MemoryIdentity.build(
        tenant_id="ACME",
        principal_id="svc-1",
        session_id="session-1",
        user_id="alice@example.com",
        project_id="proj-1",
    )

    assert identity.owner() == MemoryOwner(
        tenant_id="ACME",
        project_id="proj-1",
        principal_id="svc-1",
        user_id="alice@example.com",
        session_id="session-1",
    )
    assert identity.conversation() == ConversationIdentity(
        "ACME", "svc-1", "session-1", "alice@example.com"
    )


def test_identity_defaults_user_to_the_principal_and_leaves_project_unset() -> None:
    identity = MemoryIdentity.build(tenant_id="ACME", principal_id="svc-1", session_id="s")

    assert identity.user_id == "svc-1"
    assert identity.project_id is None
    assert identity.owner().project_id is None
    assert identity.conversation().user_id == "svc-1"
