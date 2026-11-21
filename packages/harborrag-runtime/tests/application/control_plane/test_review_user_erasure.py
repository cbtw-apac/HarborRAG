"""Erasing a user removes every session and stored completion, not only those with memories."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.ports.conversation import (
    MAX_CONVERSATION_PAGE_LIMIT,
    ConversationIdentity,
    ConversationTurn,
)
from harborrag_core.ports.memory import MemoryOwner, MemoryScope
from harborrag_core.testing.control_plane_fakes import FakePendingEffectRepository
from harborrag_memory.administration import MemoryAdministrationService
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory
from harborrag_runtime.testing.memory_fakes import FakeMemoryIndex, FakeMemoryStore, memory

pytestmark = pytest.mark.unit

TENANT = "DEFAULT"
ERASED = "user-erased"
KEPT = "user-kept"
SESSION_COUNT = MAX_CONVERSATION_PAGE_LIMIT + 1


@pytest.fixture
def stores() -> SimpleNamespace:
    return SimpleNamespace(
        memories=FakeMemoryStore(),
        index=FakeMemoryIndex(),
        conversations=InMemoryConversationMemory(),
        pending_effects=FakePendingEffectRepository(),
    )


def _administration(stores: SimpleNamespace) -> MemoryAdministrationService:
    return MemoryAdministrationService(
        conversations=stores.conversations,
        memories=stores.memories,
        index=stores.index,
        pending_effects=stores.pending_effects,
    )


def _identity(user_id: str, session_id: str) -> ConversationIdentity:
    return ConversationIdentity(TENANT, "principal-1", session_id, user_id)


async def _claim(stores: SimpleNamespace, user_id: str, key: str) -> str:
    claim = await stores.conversations.claim_completion(
        tenant_id=TENANT, user_id=user_id, key=key, request_hash="hash-1"
    )
    return str(claim.status)


async def _converse(stores: SimpleNamespace, identity: ConversationIdentity) -> None:
    await stores.conversations.create(identity, title="Which runbook covers rollback?")
    await stores.conversations.append(
        identity, ConversationTurn("Which runbook covers rollback?", "DEMO-2 covers it.")
    )


@pytest.mark.asyncio
async def test_erase_user_deletes_sessions_that_produced_no_memories(
    stores: SimpleNamespace,
) -> None:
    for index in range(SESSION_COUNT):
        await _converse(stores, _identity(ERASED, f"ses_{index:03}"))
    kept = _identity(KEPT, "ses_kept")
    await _converse(stores, kept)
    for user_id, key, session_id in (
        (ERASED, "in-session", "ses_000"),
        (ERASED, "one-off", None),
        (KEPT, "in-session", "ses_kept"),
    ):
        await _claim(stores, user_id, key)
        await stores.conversations.finish_completion(
            tenant_id=TENANT,
            user_id=user_id,
            key=key,
            request_hash="hash-1",
            response_json='{"answer":"DEMO-2 covers it."}',
            session_id=session_id,
        )

    report = await _administration(stores).erase_user(
        MemoryOwner(tenant_id=TENANT, user_id=ERASED), actor="admin-1"
    )

    assert report.as_dict() == {
        "memories": 0,
        "index_points": 0,
        "sessions": SESSION_COUNT,
        "conversation_messages_cleared": SESSION_COUNT,
        "completion_requests": 2,
    }
    listing = await stores.conversations.list_conversations(tenant_id=TENANT, user_id=ERASED)
    assert listing.conversations == ()
    assert not await stores.conversations.exists(_identity(ERASED, "ses_000"))
    assert await _claim(stores, ERASED, "in-session") == "claimed"
    assert await _claim(stores, ERASED, "one-off") == "claimed"
    assert await stores.conversations.exists(kept)
    assert len(await stores.conversations.recent_messages(kept, limit=10)) == 2
    assert await _claim(stores, KEPT, "in-session") == "completed"


@pytest.mark.asyncio
async def test_erase_user_counts_the_memories_of_listed_and_already_deleted_sessions(
    stores: SimpleNamespace,
) -> None:
    await _converse(stores, _identity(ERASED, "ses_live"))
    rows = (
        memory("session-live", user_id=ERASED, scope=MemoryScope.SESSION, session_id="ses_live"),
        memory("session-gone", user_id=ERASED, scope=MemoryScope.SESSION, session_id="ses_gone"),
        memory("user-fact", user_id=ERASED, source_session_id="ses_gone"),
        memory("other-user", user_id=KEPT),
    )
    for row in rows:
        await stores.memories.save(row)

    report = await _administration(stores).erase_user(
        MemoryOwner(tenant_id=TENANT, user_id=ERASED), actor="admin-1"
    )

    assert (report.memories, report.index_points, report.sessions) == (3, 3, 1)
    assert set(stores.memories.rows) == {"other-user"}
    assert not await stores.conversations.exists(_identity(ERASED, "ses_live"))
