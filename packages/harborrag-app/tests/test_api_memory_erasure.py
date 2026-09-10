"""Session and right-to-erasure endpoints report exactly what they removed."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from app_test_fixtures import MockAppService
from app_test_memory import memory
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.base import utc_now
from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage
from harborrag_core.ports.memory import MemoryScope

_ADMIN_LOG = "harborrag.app.workflow_control.memory.administration"


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _identity(session_id: str = "session-1") -> ConversationIdentity:
    return ConversationIdentity("DEFAULT", "dev", session_id, "dev")


async def _seed_session(service: MockAppService, session_id: str = "session-1") -> None:
    identity = _identity(session_id)
    await service.conversations.create(identity, kind="chat")
    await service.conversations.append_messages(
        identity,
        (
            ConversationMessage("msg-1", "user", "where is the runbook?", utc_now()),
            ConversationMessage("msg-2", "assistant", "in the wiki", utc_now()),
        ),
    )


@pytest.mark.asyncio
async def test_session_erasure_removes_messages_memories_and_vectors(
    client: TestClient,
    service: MockAppService,
) -> None:
    await _seed_session(service)
    await service.memory_store.save(
        memory("mem-session", scope=MemoryScope.SESSION, session_id="session-1")
    )
    await service.memory_store.save(memory("mem-user"))

    response = client.delete("/v1/memory/sessions/session-1")

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "memories": 1,
        "index_points": 1,
        "sessions": 1,
        "conversation_messages_cleared": 1,
        "agent_run_checkpoints": 0,
    }
    assert await service.conversations.recent_messages(_identity(), limit=10) == ()
    assert service.memory_index.deleted == ["mem-session"]
    # A user-scoped memory outlives one session; only the session's own go.
    assert "mem-user" in service.memory_store.rows


def test_erasing_an_unknown_session_is_a_not_found(client: TestClient) -> None:
    response = client.delete("/v1/memory/sessions/session-absent")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.asyncio
async def test_user_erasure_reaches_sessions_named_by_the_users_memories(
    client: TestClient,
    service: MockAppService,
) -> None:
    await _seed_session(service, "session-1")
    await service.memory_store.save(memory("mem-user", source_session_id="session-1"))
    await service.memory_store.save(
        memory("mem-session", scope=MemoryScope.SESSION, session_id="session-1")
    )
    await service.memory_store.save(memory("mem-other-user", user_id="someone-else"))

    response = client.delete("/v1/memory/users/dev")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "dev",
        "memories": 2,
        "index_points": 2,
        "sessions": 1,
        "conversation_messages_cleared": 1,
        "agent_run_checkpoints": 0,
    }
    assert sorted(service.memory_index.deleted) == ["mem-session", "mem-user"]
    assert list(service.memory_store.rows) == ["mem-other-user"]
    assert await service.conversations.recent_messages(_identity(), limit=10) == ()


@pytest.mark.asyncio
async def test_user_erasure_logs_counts_and_never_content(
    client: TestClient,
    service: MockAppService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await service.memory_store.save(memory("mem-user", content="lives on Rue Secrète 4"))

    with caplog.at_level(logging.INFO, logger=_ADMIN_LOG):
        assert client.delete("/v1/memory/users/dev").status_code == 200

    records = [r for r in caplog.records if "Memory erasure" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "tenant=DEFAULT" in message
    assert "actor=dev" in message
    assert "target=dev" in message
    assert "memories=1" in message
    assert "Rue Secrète" not in caplog.text


@pytest.mark.asyncio
async def test_a_user_with_nothing_remembered_erases_to_zero(client: TestClient) -> None:
    response = client.delete("/v1/memory/users/nobody")

    assert response.status_code == 200
    assert response.json()["memories"] == 0
    assert response.json()["sessions"] == 0


@pytest.mark.asyncio
async def test_an_index_failure_still_erases_the_canonical_row(
    client: TestClient,
    service: MockAppService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service.memory_index.failure = RuntimeError("qdrant gone")
    await service.memory_store.save(memory("mem-user"))

    with caplog.at_level(logging.WARNING, logger=_ADMIN_LOG):
        response = client.delete("/v1/memory/users/dev")

    assert response.status_code == 200
    assert response.json()["memories"] == 1
    assert response.json()["index_points"] == 0
    assert service.memory_store.rows == {}
    assert "Memory index delete failed" in caplog.text
