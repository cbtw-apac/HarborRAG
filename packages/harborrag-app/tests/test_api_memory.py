"""Memory administration routes: the caller only ever sees their own rows."""

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
from harborrag_app.workflow_control.memory import MemoryAdministrationService
from harborrag_core.ports.memory import MemoryScope

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


def _token(role: str, *, subject: str = "p1", tenants: tuple[str, ...] = ("DEFAULT",)) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "oid": "alice",
            "role": role,
            "tenants": list(tenants),
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


@pytest.fixture
def hmac_client(
    monkeypatch: pytest.MonkeyPatch,
    service: MockAppService,
) -> Iterator[TestClient]:
    """An authenticated app with an end user selected by a signed claim."""

    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET, auth_user_id_claim="oid")
    with TestClient(create_fastapi_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_listing_returns_the_callers_own_memories_with_provenance(
    client: TestClient,
    service: MockAppService,
) -> None:
    await service.memory_store.save(
        memory(
            "mem-own",
            content="prefers metric units",
            source_session_id="session-1",
            source_message_ids=("msg-1", "msg-2"),
        )
    )
    await service.memory_store.save(memory("mem-foreign", user_id="someone-else"))

    response = client.get("/v1/memory/memories")

    assert response.status_code == 200
    memories = response.json()["memories"]
    assert [item["memory_id"] for item in memories] == ["mem-own"]
    assert memories[0]["source_session_id"] == "session-1"
    assert memories[0]["source_message_ids"] == ["msg-1", "msg-2"]
    assert memories[0]["scope"] == "user"
    assert memories[0]["valid_from"] is not None


@pytest.mark.asyncio
async def test_signed_user_claim_selects_memory_and_request_field_cannot_override_it(
    hmac_client: TestClient,
    service: MockAppService,
) -> None:
    """An HTTP caller sees its signed user's rows, never a body or query user."""

    await service.memory_store.save(memory("mem-default", principal_id="p1"))
    await service.memory_store.save(memory("mem-alice", user_id="alice", principal_id="p1"))

    response = hmac_client.get(
        "/v1/memory/memories",
        params={"user_id": "someone-else"},
        headers={"Authorization": f"Bearer {_token('reader')}"},
    )

    assert response.status_code == 200
    assert [item["memory_id"] for item in response.json()["memories"]] == ["mem-alice"]


@pytest.mark.asyncio
async def test_a_scope_filter_narrows_the_listing(
    client: TestClient,
    service: MockAppService,
) -> None:
    await service.memory_store.save(memory("mem-user"))
    await service.memory_store.save(
        memory("mem-session", scope=MemoryScope.SESSION, session_id="session-1")
    )

    listed = client.get(
        "/v1/memory/memories",
        params={"scope": "session", "session_id": "session-1"},
    )
    users_only = client.get("/v1/memory/memories", params={"scope": "user"})

    assert [item["memory_id"] for item in listed.json()["memories"]] == ["mem-session"]
    assert [item["memory_id"] for item in users_only.json()["memories"]] == ["mem-user"]


def test_the_listing_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/v1/memory/memories", params={"limit": 0}).status_code == 422
    assert client.get("/v1/memory/memories", params={"limit": 101}).status_code == 422


@pytest.mark.asyncio
async def test_deleting_an_own_memory_also_drops_its_index_point(
    client: TestClient,
    service: MockAppService,
) -> None:
    await service.memory_store.save(memory("mem-own"))

    response = client.delete("/v1/memory/memories/mem-own")

    assert response.status_code == 200
    assert response.json() == {"memory_id": "mem-own", "deleted": True}
    assert service.memory_index.deleted == ["mem-own"]
    assert "mem-own" not in service.memory_store.rows


@pytest.mark.asyncio
async def test_deleting_a_foreign_memory_is_a_not_found(
    client: TestClient,
    service: MockAppService,
) -> None:
    await service.memory_store.save(memory("mem-foreign", user_id="someone-else"))

    response = client.delete("/v1/memory/memories/mem-foreign")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"
    assert "mem-foreign" in service.memory_store.rows
    assert service.memory_index.deleted == []


def test_every_route_requires_a_bearer_token(hmac_client: TestClient) -> None:
    for method, path in (
        ("get", "/v1/memory/memories"),
        ("delete", "/v1/memory/memories/mem-1"),
        ("delete", "/v1/memory/sessions/session-1"),
        ("delete", "/v1/memory/users/alice"),
    ):
        response = getattr(hmac_client, method)(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "harbor_auth_error"


def test_user_erasure_requires_the_admin_role(hmac_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {_token('reader')}"}

    response = hmac_client.delete("/v1/memory/users/alice", headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "requires admin role"


def test_a_reader_may_read_and_erase_their_own_session(hmac_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {_token('reader')}"}

    assert hmac_client.get("/v1/memory/memories", headers=headers).status_code == 200
    # The session does not exist for this caller, which is a 404 rather than
    # the 403 an insufficient role would produce.
    assert hmac_client.delete("/v1/memory/sessions/session-1", headers=headers).status_code == 404


def test_a_deployment_without_a_memory_store_reports_unavailable(
    client: TestClient,
    service: MockAppService,
) -> None:
    service._memory_admin = MemoryAdministrationService(conversations=service.conversations)

    response = client.get("/v1/memory/memories")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "harbor_unavailable_error"


def test_a_foreign_tenant_is_refused(hmac_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {_token('admin')}"}

    for path in ("/v1/memory/memories", "/v1/memory/users/alice"):
        method = hmac_client.get if path.endswith("memories") else hmac_client.delete
        response = method(path, params={"tenant": "OTHER"}, headers=headers)
        assert response.status_code == 403, path
        assert response.json()["error"]["message"] == "tenant access is not permitted"


def _project_memory(memory_id: str, *, user_id: str, principal_id: str = "p2"):
    return memory(
        memory_id,
        scope=MemoryScope.PROJECT,
        project_id="proj-1",
        user_id=user_id,
        principal_id=principal_id,
        content="the atlas project ships on fridays",
    )


@pytest.mark.asyncio
async def test_a_reader_cannot_delete_a_shared_memory_someone_else_wrote(
    hmac_client: TestClient,
    service: MockAppService,
) -> None:
    await service.memory_store.save(_project_memory("mem-proj", user_id="bob"))

    response = hmac_client.delete(
        "/v1/memory/memories/mem-proj",
        params={"project_id": "proj-1"},
        headers={"Authorization": f"Bearer {_token('reader')}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "harbor_auth_error"
    assert "mem-proj" in service.memory_store.rows
    assert service.memory_index.deleted == []


@pytest.mark.asyncio
async def test_a_shared_credential_does_not_make_a_colleague_the_author(
    hmac_client: TestClient,
    service: MockAppService,
) -> None:
    """Review focus: same principal (p1), different signed end user."""
    await service.memory_store.save(_project_memory("mem-proj", user_id="bob", principal_id="p1"))

    response = hmac_client.delete(
        "/v1/memory/memories/mem-proj",
        params={"project_id": "proj-1"},
        headers={"Authorization": f"Bearer {_token('reader', subject='p1')}"},
    )

    assert response.status_code == 403
    assert "mem-proj" in service.memory_store.rows


@pytest.mark.asyncio
async def test_the_author_of_a_shared_memory_may_delete_it_as_a_reader(
    hmac_client: TestClient,
    service: MockAppService,
) -> None:
    # _token's "oid" claim is "alice", and the hmac client maps it to user_id.
    await service.memory_store.save(
        _project_memory("mem-own-proj", user_id="alice", principal_id="p1")
    )

    response = hmac_client.delete(
        "/v1/memory/memories/mem-own-proj",
        params={"project_id": "proj-1"},
        headers={"Authorization": f"Bearer {_token('reader')}"},
    )

    assert response.status_code == 200
    assert "mem-own-proj" not in service.memory_store.rows
    assert service.memory_index.deleted == ["mem-own-proj"]


@pytest.mark.asyncio
async def test_an_admin_may_delete_a_shared_memory_someone_else_wrote(
    hmac_client: TestClient,
    service: MockAppService,
) -> None:
    await service.memory_store.save(_project_memory("mem-proj", user_id="bob"))

    response = hmac_client.delete(
        "/v1/memory/memories/mem-proj",
        params={"project_id": "proj-1"},
        headers={"Authorization": f"Bearer {_token('admin')}"},
    )

    assert response.status_code == 200
    assert "mem-proj" not in service.memory_store.rows
    assert service.memory_index.deleted == ["mem-proj"]


@pytest.mark.asyncio
async def test_a_reader_still_deletes_their_own_user_memory(
    hmac_client: TestClient,
    service: MockAppService,
) -> None:
    await service.memory_store.save(memory("mem-mine", user_id="alice", principal_id="p1"))

    response = hmac_client.delete(
        "/v1/memory/memories/mem-mine",
        headers={"Authorization": f"Bearer {_token('reader')}"},
    )

    assert response.status_code == 200
    assert "mem-mine" not in service.memory_store.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["editor", "admin"])
async def test_an_editor_in_another_tenant_cannot_delete_a_global_memory(
    hmac_client: TestClient,
    service: MockAppService,
    role: str,
) -> None:
    """GLOBAL rows are visible to every tenant, so the editor override must not reach them."""
    await service.memory_store.save(
        memory("mem-global", scope=MemoryScope.GLOBAL, tenant_id="DEFAULT", user_id="bob")
    )

    response = hmac_client.delete(
        "/v1/memory/memories/mem-global",
        params={"tenant": "TENANT-B"},
        headers={"Authorization": f"Bearer {_token(role, tenants=('TENANT-B',))}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "harbor_auth_error"
    assert "mem-global" in service.memory_store.rows
    assert service.memory_index.deleted == []


@pytest.mark.asyncio
async def test_an_editor_still_deletes_a_tenant_memory_in_their_own_tenant(
    hmac_client: TestClient,
    service: MockAppService,
) -> None:
    await service.memory_store.save(
        memory("mem-tenant", scope=MemoryScope.TENANT, tenant_id="DEFAULT", user_id="bob")
    )

    response = hmac_client.delete(
        "/v1/memory/memories/mem-tenant",
        headers={"Authorization": f"Bearer {_token('editor')}"},
    )

    assert response.status_code == 200
    assert "mem-tenant" not in service.memory_store.rows
