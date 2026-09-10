"""Renaming and deleting the caller's own conversations over HTTP."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app_test_conversations import (
    ALICE,
    BOB,
    CONVERSATIONS,
    auth,
    hmac_app,
    identity,
    message,
    seed,
)
from app_test_fixtures import MockAppService
from app_test_memory import memory
from fastapi.testclient import TestClient

from harborrag_core.ports.memory import MemoryOwner, MemoryScope


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    with TestClient(hmac_app(monkeypatch, service)) as test_client:
        yield test_client


def _path(session_id: str = "session-1") -> str:
    return f"{CONVERSATIONS}/{session_id}"


async def _title(service: MockAppService, session_id: str = "session-1") -> str | None:
    page = await service.conversations.list_conversations(tenant_id="DEFAULT", user_id="alice")
    return next(row.title for row in page.conversations if row.session_id == session_id)


@pytest.mark.asyncio
async def test_renaming_stores_the_trimmed_title(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", title="Untitled")

    response = client.patch(_path(), json={"title": "  Runbook triage  "}, headers=auth())

    assert response.status_code == 200
    assert response.json() == {"session_id": "session-1", "title": "Runbook triage"}
    assert await _title(service) == "Runbook triage"


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", "   "])
async def test_a_blank_title_clears_the_name(
    client: TestClient,
    service: MockAppService,
    title: str,
) -> None:
    await seed(service, "session-1", title="Runbook triage")

    response = client.patch(_path(), json={"title": title}, headers=auth())

    assert response.status_code == 200
    assert response.json() == {"session_id": "session-1", "title": None}
    assert await _title(service) is None


def test_renaming_an_unknown_conversation_is_a_not_found(client: TestClient) -> None:
    response = client.patch(_path("session-absent"), json={"title": "x"}, headers=auth())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.asyncio
async def test_a_second_user_cannot_rename_the_first_users_conversation(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", ALICE, title="Alice only")

    response = client.patch(_path(), json={"title": "hijacked"}, headers=auth(BOB))

    assert response.status_code == 404
    assert await _title(service) == "Alice only"


@pytest.mark.asyncio
async def test_a_forged_owner_body_field_is_rejected_outright(
    client: TestClient,
    service: MockAppService,
) -> None:
    """The rename body carries a title and nothing else."""

    await seed(service, "session-1", title="Alice only")

    response = client.patch(
        _path(),
        json={"title": "renamed", "user_id": "bob", "principal_id": "cred-2"},
        headers=auth(),
    )

    assert response.status_code == 422
    assert await _title(service) == "Alice only"


@pytest.mark.asyncio
async def test_renaming_requires_a_bearer_token(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1")

    response = client.patch(_path(), json={"title": "x"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_renaming_refuses_a_foreign_tenant(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1")

    response = client.patch(
        _path(),
        params={"tenant": "OTHER"},
        json={"title": "x"},
        headers=auth(),
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_deleting_removes_the_messages_memories_and_vector_points(
    client: TestClient,
    service: MockAppService,
) -> None:
    """The delete reuses the memory erasure, so extractions go with the turns."""

    await seed(service, "session-1", messages=(message("msg-1"), message("msg-2", "assistant")))
    await service.memory_store.save(
        memory("mem-session", scope=MemoryScope.SESSION, user_id="alice", session_id="session-1")
    )
    await service.memory_store.save(memory("mem-user", user_id="alice"))

    response = client.delete(_path(), headers=auth())

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "memories": 1,
        "index_points": 1,
        "sessions": 1,
        "conversation_messages_cleared": 1,
        "agent_run_checkpoints": 0,
    }
    assert await service.conversations.recent_messages(identity("session-1"), limit=10) == ()
    assert service.memory_index.deleted == ["mem-session"]
    # A user-scoped memory outlives one conversation.
    assert "mem-user" in service.memory_store.rows


@pytest.mark.asyncio
async def test_deleting_goes_through_the_memory_administration_service(
    client: TestClient,
    service: MockAppService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One erasure path, shared with DELETE /v1/memory/sessions/{session_id}."""

    await seed(service, "session-1", messages=(message("msg-1"),))
    calls: list[tuple[MemoryOwner, str]] = []
    original = service._memory_admin.erase_session

    async def spy(owner: MemoryOwner, *, actor: str) -> object:
        calls.append((owner, actor))
        return await original(owner, actor=actor)

    monkeypatch.setattr(service._memory_admin, "erase_session", spy)

    assert client.delete(_path(), headers=auth()).status_code == 200

    owner, actor = calls[0]
    assert (owner.tenant_id, owner.user_id, owner.session_id) == ("DEFAULT", "alice", "session-1")
    # The credential that acted is recorded as the actor, not as the owner.
    assert (owner.principal_id, actor) == ("cred-1", "cred-1")


def test_deleting_an_unknown_conversation_is_a_not_found(client: TestClient) -> None:
    response = client.delete(_path("session-absent"), headers=auth())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.asyncio
async def test_a_second_user_cannot_delete_the_first_users_conversation(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", ALICE, messages=(message("msg-1"),))

    response = client.delete(_path(), headers=auth(BOB))

    assert response.status_code == 404
    messages = await service.conversations.recent_messages(identity("session-1"), limit=10)
    assert [row.message_id for row in messages] == ["msg-1"]


@pytest.mark.asyncio
async def test_deleting_requires_a_bearer_token(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1")

    response = client.delete(_path())

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_deleting_refuses_a_foreign_tenant(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1")

    response = client.delete(_path(), params={"tenant": "OTHER"}, headers=auth())

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_deleted_conversation_stops_being_listed(
    client: TestClient,
    service: MockAppService,
) -> None:
    """Delete must remove the conversation, not just empty it.

    Clearing messages alone left the session row behind, so a conversation the
    user deleted kept appearing in their list with no messages. Erasure now
    deletes the session itself.
    """

    await seed(service, "session-1", title="Runbooks", messages=(message("msg-1"),))

    assert client.delete(_path(), headers=auth()).status_code == 200

    rows = client.get(CONVERSATIONS, headers=auth()).json()["conversations"]
    assert rows == []
