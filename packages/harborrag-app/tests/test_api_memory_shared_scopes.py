"""Shared PROJECT/TENANT memories: readable across the tenant, deletable by their author.

A non-author may delete one only with a role that moderates its scope: editor and above
for PROJECT, admin and above for TENANT.

Recall shares project memory with everyone working in the project, so listing it is by
design. What another user may not do is learn which of the author's sessions and
messages produced it, or erase it for everyone.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from app_test_memory import memory
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import HarborAuthError
from harborrag_core.ports.memory import MemoryOwner, MemoryScope

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
PROJECT = {"tenant": "T", "project_id": "P"}


def _headers(user: str, *, role: str = "reader") -> dict[str, str]:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": f"principal-{user}",
            "oid": user,
            "role": role,
            "tenants": ["T"],
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET, auth_user_id_claim="oid")
    with TestClient(create_fastapi_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


async def _alices_project_fact(service: MockAppService, scope: MemoryScope) -> None:
    await service.memory_store.save(
        memory(
            "mem-shared",
            scope=scope,
            tenant_id="T",
            project_id="P",
            user_id="alice",
            principal_id="principal-alice",
            source_session_id="sess-a",
            source_message_ids=("msg-1",),
            content="A project fact",
        )
    )


@pytest.mark.asyncio
async def test_another_user_sees_a_project_memory_without_its_provenance(
    client: TestClient, service: MockAppService
) -> None:
    await _alices_project_fact(service, MemoryScope.PROJECT)

    bob = client.get(
        "/v1/memory/memories", params={**PROJECT, "scope": "project"}, headers=_headers("bob")
    )
    alice = client.get(
        "/v1/memory/memories", params={**PROJECT, "scope": "project"}, headers=_headers("alice")
    )

    assert bob.status_code == 200
    [row] = bob.json()["memories"]
    assert row["content"] == "A project fact"
    assert row["source_session_id"] is None and row["source_message_ids"] == []
    [own] = alice.json()["memories"]
    assert own["source_session_id"] == "sess-a" and own["source_message_ids"] == ["msg-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "scope", "allowed"),
    [
        ("reader", MemoryScope.PROJECT, False),
        ("reader", MemoryScope.TENANT, False),
        ("editor", MemoryScope.PROJECT, True),
        ("editor", MemoryScope.TENANT, False),
        ("admin", MemoryScope.PROJECT, True),
        ("admin", MemoryScope.TENANT, True),
        ("owner", MemoryScope.TENANT, True),
    ],
)
async def test_deleting_another_users_shared_memory_needs_a_role_that_moderates_its_scope(
    client: TestClient, service: MockAppService, role: str, scope: MemoryScope, allowed: bool
) -> None:
    await _alices_project_fact(service, scope)

    response = client.delete(
        "/v1/memory/memories/mem-shared", params=PROJECT, headers=_headers("bob", role=role)
    )

    if allowed:
        assert response.status_code == 200
        assert "mem-shared" not in service.memory_store.rows
        assert service.memory_index.deleted == ["mem-shared"]
    else:
        assert response.status_code == 403
        assert "mem-shared" in service.memory_store.rows
        assert service.memory_index.deleted == []


@pytest.mark.asyncio
async def test_the_author_may_delete_their_shared_memory(
    client: TestClient, service: MockAppService
) -> None:
    await _alices_project_fact(service, MemoryScope.PROJECT)

    response = client.delete(
        "/v1/memory/memories/mem-shared", params=PROJECT, headers=_headers("alice")
    )

    assert response.status_code == 200
    assert "mem-shared" not in service.memory_store.rows


@pytest.mark.asyncio
async def test_a_granted_scope_lets_a_non_author_delete_the_shared_row(
    service: MockAppService,
) -> None:
    """The service-level grant the route derives from the caller's role."""

    await _alices_project_fact(service, MemoryScope.PROJECT)
    bob = MemoryOwner(tenant_id="T", project_id="P", principal_id="principal-bob", user_id="bob")
    admin = service._memory_admin

    with pytest.raises(HarborAuthError):
        await admin.delete_memory(bob, "mem-shared", shared_scopes=frozenset({MemoryScope.TENANT}))
    await admin.delete_memory(bob, "mem-shared", shared_scopes=frozenset({MemoryScope.PROJECT}))

    assert "mem-shared" not in service.memory_store.rows
