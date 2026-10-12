"""Session and right-to-erasure endpoints report exactly what they removed."""

from __future__ import annotations

import asyncio
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
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage
from harborrag_core.ports.memory import MemoryScope

_ADMIN_LOG = "harborrag.app.workflow_control.memory.administration"
# The subject of a right-to-erasure run: a named end user, never the shared
# DEFAULT_USER sentinel the test principal itself acts under.
_SUBJECT = "user-a"


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _identity(session_id: str = "session-1", user_id: str = DEFAULT_USER) -> ConversationIdentity:
    return ConversationIdentity("DEFAULT", "dev", session_id, user_id)


async def _seed_session(
    service: MockAppService, session_id: str = "session-1", user_id: str = DEFAULT_USER
) -> None:
    identity = _identity(session_id, user_id)
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
        "deferred_index_points": 0,
        "status": "completed",
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
async def test_user_erasure_deletes_every_session_and_memory_of_the_user(
    client: TestClient,
    service: MockAppService,
) -> None:
    await _seed_session(service, "session-1", _SUBJECT)
    await _seed_session(service, "session-2", _SUBJECT)  # produced no memory at all
    await service.memory_store.save(
        memory("mem-user", user_id=_SUBJECT, source_session_id="session-1")
    )
    await service.memory_store.save(
        memory("mem-session", user_id=_SUBJECT, scope=MemoryScope.SESSION, session_id="session-1")
    )
    await service.memory_store.save(memory("mem-other-user", user_id="someone-else"))

    response = client.delete(f"/v1/memory/users/{_SUBJECT}")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "user_id": _SUBJECT,
        "erasure_id": body["erasure_id"],
        "status": "completed",
        "sessions": 2,
        "memories": 2,
        "index_points": 2,
        "deferred_index_points": 0,
        "agent_run_checkpoints": 0,
        "completion_requests": 0,
        "model_usage_pseudonymized": 0,
    }
    for session_id in ("session-1", "session-2"):
        assert not await service.conversations.exists(_identity(session_id, _SUBJECT))
    assert list(service.memory_store.rows) == ["mem-other-user"]
    record = service.erasure_journal.records[body["erasure_id"]]
    assert (record.status, record.actor, record.subject_user_id) == (
        "completed",
        "dev",
        _SUBJECT,
    )


@pytest.mark.asyncio
async def test_a_busy_session_fails_the_erasure_but_the_rest_is_erased(
    client: TestClient,
    service: MockAppService,
) -> None:
    await _seed_session(service, "session-1", _SUBJECT)
    await service.memory_store.save(memory("mem-user", user_id=_SUBJECT))
    busy = _identity("session-1", _SUBJECT)
    # Acquire in a child task so this test's own context holds no fencing token:
    # the erasure must meet the lease the way an unrelated request would.
    assert await asyncio.create_task(
        service.conversations.acquire_turn_lease(busy, token="turn-1", lease_seconds=60)
    )

    response = client.delete(f"/v1/memory/users/{_SUBJECT}")

    assert response.status_code == 409
    assert "sessions" in response.json()["error"]["message"]
    assert service.memory_store.rows == {}
    [record] = service.erasure_journal.records.values()
    assert (record.status, record.failed_steps) == ("failed", ("sessions",))
    assert record.erasure_id in response.json()["error"]["message"]

    await service.conversations.release_turn_lease(busy, token="turn-1")
    retry = client.delete(f"/v1/memory/users/{_SUBJECT}")
    assert retry.status_code == 200
    assert retry.json()["sessions"] == 1


@pytest.mark.asyncio
async def test_erasing_the_shared_default_user_is_refused_and_touches_nothing(
    client: TestClient,
    service: MockAppService,
) -> None:
    """Every user-less caller shares DEFAULT_USER; erasing it would wipe all of them."""

    await _seed_session(service, "session-1")
    await service.memory_store.save(memory("mem-user"))

    response = client.delete(f"/v1/memory/users/{DEFAULT_USER}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"
    assert await service.conversations.exists(_identity())
    assert list(service.memory_store.rows) == ["mem-user"]
    assert service.erasure_journal.records == {}


@pytest.mark.asyncio
async def test_user_erasure_never_reaches_another_tenant(
    client: TestClient,
    service: MockAppService,
) -> None:
    other = ConversationIdentity("OTHER", "dev", "session-1", _SUBJECT)
    await service.conversations.create(other, kind="chat")
    await service.memory_store.save(memory("mem-other", tenant_id="OTHER", user_id=_SUBJECT))
    await service.memory_store.save(memory("mem-mine", user_id=_SUBJECT))

    response = client.delete(f"/v1/memory/users/{_SUBJECT}")

    assert response.status_code == 200
    assert await service.conversations.exists(other)
    assert list(service.memory_store.rows) == ["mem-other"]


@pytest.mark.asyncio
async def test_a_session_delete_with_a_queued_vector_delete_reports_deferred(
    client: TestClient,
    service: MockAppService,
) -> None:
    await _seed_session(service)
    await service.memory_store.save(
        memory("mem-session", scope=MemoryScope.SESSION, session_id="session-1")
    )
    service.memory_index.failure = RuntimeError("qdrant gone")

    response = client.delete("/v1/memory/sessions/session-1")

    assert response.status_code == 200
    assert response.json()["status"] == "deferred"
    assert response.json()["deferred_index_points"] == 1
    assert service.memory_store.rows == {}


@pytest.mark.asyncio
async def test_user_erasure_reaches_project_scoped_memories_but_only_the_targets(
    client: TestClient,
    service: MockAppService,
) -> None:
    """Right-to-erasure must reach a project-scoped fact about the user.

    PROJECT scope keys on (tenant_id, project_id) with no user, so neither the
    visibility query nor ``delete``'s own re-check can reach it for an eraser
    holding no project_id -- it survived erasure and stayed recallable to
    everyone else in the project. The second assertion is the guard that
    matters just as much: erasing by project instead would have taken a
    different user's project memory with it.
    """

    await service.memory_store.save(
        memory("mem-project", user_id=_SUBJECT, scope=MemoryScope.PROJECT, project_id="atlas")
    )
    await service.memory_store.save(
        memory(
            "mem-project-other",
            scope=MemoryScope.PROJECT,
            project_id="atlas",
            user_id="someone-else",
        )
    )

    response = client.delete(f"/v1/memory/users/{_SUBJECT}")

    assert response.status_code == 200
    assert response.json()["memories"] == 1
    assert service.memory_index.deleted == ["mem-project"]
    assert list(service.memory_store.rows) == ["mem-project-other"]


@pytest.mark.asyncio
async def test_user_erasure_logs_counts_and_never_content(
    client: TestClient,
    service: MockAppService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await service.memory_store.save(
        memory("mem-user", user_id=_SUBJECT, content="lives on Rue Secrète 4")
    )

    with caplog.at_level(logging.INFO, logger=_ADMIN_LOG):
        assert client.delete(f"/v1/memory/users/{_SUBJECT}").status_code == 200

    records = [r for r in caplog.records if "Memory erasure" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "tenant=DEFAULT" in message
    assert "actor=dev" in message
    assert f"target={_SUBJECT}" in message
    assert "memories=1" in message
    assert "status=completed" in message
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
    await service.memory_store.save(memory("mem-user", user_id=_SUBJECT))

    with caplog.at_level(logging.WARNING, logger=_ADMIN_LOG):
        response = client.delete(f"/v1/memory/users/{_SUBJECT}")

    assert response.status_code == 202
    assert response.json()["status"] == "deferred"
    assert response.json()["deferred_index_points"] == 1
    assert response.json()["memories"] == 1
    assert response.json()["index_points"] == 0
    assert service.memory_store.rows == {}
    assert "Memory index delete failed" in caplog.text


@pytest.mark.asyncio
async def test_deleting_a_session_removes_the_users_facts_extracted_from_it(
    client: TestClient,
    service: MockAppService,
) -> None:
    await _seed_session(service, "session-1")
    await service.memory_store.save(memory("mem-from-1", source_session_id="session-1"))
    await service.memory_store.save(memory("mem-from-2", source_session_id="session-2"))
    await service.memory_store.save(
        memory("mem-other-user", user_id="someone-else", source_session_id="session-1")
    )

    response = client.delete("/v1/memory/sessions/session-1")

    assert response.status_code == 200
    assert response.json()["memories"] == 1
    assert sorted(service.memory_store.rows) == ["mem-from-2", "mem-other-user"]
    assert service.memory_index.deleted == ["mem-from-1"]


@pytest.mark.asyncio
async def test_deleting_a_session_removes_the_users_shared_scope_facts_from_it(
    client: TestClient,
    service: MockAppService,
) -> None:
    await _seed_session(service, "session-1")
    await service.memory_store.save(
        memory(
            "mem-project",
            scope=MemoryScope.PROJECT,
            project_id="project-a",
            source_session_id="session-1",
        )
    )
    await service.memory_store.save(
        memory("mem-tenant", scope=MemoryScope.TENANT, source_session_id="session-1")
    )

    response = client.delete("/v1/memory/sessions/session-1")

    assert response.status_code == 200
    assert response.json()["memories"] == 2
    assert not service.memory_store.rows


@pytest.mark.asyncio
async def test_deleting_a_session_never_removes_another_users_shared_facts(
    client: TestClient,
    service: MockAppService,
) -> None:
    await _seed_session(service, "session-1")
    await service.memory_store.save(
        memory(
            "mem-other-project",
            user_id="someone-else",
            scope=MemoryScope.PROJECT,
            project_id="project-a",
            source_session_id="session-1",
        )
    )
    await service.memory_store.save(
        memory(
            "mem-other-principal",
            principal_id="another-caller",
            source_session_id="session-1",
        )
    )

    response = client.delete("/v1/memory/sessions/session-1")

    assert response.status_code == 200
    assert response.json()["memories"] == 0
    assert sorted(service.memory_store.rows) == ["mem-other-principal", "mem-other-project"]
    assert service.memory_index.deleted == []
