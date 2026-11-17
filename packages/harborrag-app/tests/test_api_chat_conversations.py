"""Listing the caller's own conversations over the public HTTP surface."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app_test_conversations import (
    ALICE,
    BOB,
    CONVERSATIONS,
    auth,
    hmac_app,
    message,
    seed,
)
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_core.ports.conversation import encode_conversation_cursor


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    with TestClient(hmac_app(monkeypatch, service)) as test_client:
        yield test_client


def _sessions(payload: dict[str, object]) -> list[str]:
    rows = payload["items"]
    assert isinstance(rows, list)
    return [str(row["session_id"]) for row in rows]


@pytest.mark.asyncio
async def test_listing_returns_the_callers_own_conversations(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", title="Runbooks", messages=(message("msg-1"),))

    response = client.get(CONVERSATIONS, headers=auth())

    assert response.status_code == 200
    rows = response.json()["items"]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "session-1"
    assert rows[0]["title"] == "Runbooks"
    assert rows[0]["message_count"] == 1
    assert rows[0]["created_at"] and rows[0]["updated_at"]
    # No owner field is echoed back: the caller already is the owner.
    assert set(rows[0]) == {
        "session_id",
        "title",
        "project_id",
        "profile_id",
        "created_at",
        "updated_at",
        "message_count",
    }
    assert response.json()["next_cursor"] is None


def test_a_caller_supplied_title_is_trimmed_and_kept(client: TestClient) -> None:
    created = client.post(
        "/v1/chat/sessions", json={"title": "  Quarterly report  "}, headers=auth()
    )

    assert created.status_code == 201
    assert created.json()["title"] == "Quarterly report"
    rows = client.get(CONVERSATIONS, headers=auth()).json()["items"]
    assert [row["title"] for row in rows] == ["Quarterly report"]


def test_creating_a_session_without_a_title_leaves_it_untitled(client: TestClient) -> None:
    created = client.post("/v1/chat/sessions", json={}, headers=auth())

    assert created.status_code == 201
    rows = client.get(CONVERSATIONS, headers=auth()).json()["items"]
    assert rows[0]["title"] == ""


@pytest.mark.asyncio
async def test_paging_walks_every_conversation_exactly_once(
    client: TestClient,
    service: MockAppService,
) -> None:
    for index in range(3):
        await seed(service, f"session-{index}")

    first = client.get(CONVERSATIONS, params={"limit": 2}, headers=auth()).json()
    cursor = first["next_cursor"]
    second = client.get(
        CONVERSATIONS,
        params={"limit": 2, "cursor": cursor},
        headers=auth(),
    ).json()

    assert len(_sessions(first)) == 2
    assert cursor
    assert len(_sessions(second)) == 1
    assert second["next_cursor"] is None
    assert sorted(_sessions(first) + _sessions(second)) == [
        "session-0",
        "session-1",
        "session-2",
    ]


@pytest.mark.asyncio
async def test_a_malformed_cursor_is_a_client_error_not_a_server_error(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1")

    response = client.get(CONVERSATIONS, params={"cursor": "!!!!"}, headers=auth())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_a_cursor_for_another_tenants_conversation_is_rejected(
    client: TestClient,
    service: MockAppService,
) -> None:
    """A well-formed cursor naming a foreign row must not page from the start."""

    await seed(service, "session-1")
    await seed(service, "session-bob", BOB, tenant="OTHER")

    response = client.get(
        CONVERSATIONS,
        params={"cursor": encode_conversation_cursor("session-bob")},
        headers=auth(),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_credentials_only_list_their_own_user_conversations(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", ALICE, title="Alice only")
    await seed(service, "session-bob", BOB, title="Bob only")

    alice = client.get(CONVERSATIONS, headers=auth(ALICE)).json()
    bob = client.get(CONVERSATIONS, headers=auth(BOB)).json()

    assert _sessions(alice) == ["session-1"]
    assert _sessions(bob) == ["session-bob"]


@pytest.mark.asyncio
async def test_a_forged_owner_query_field_cannot_widen_the_listing(
    client: TestClient,
    service: MockAppService,
) -> None:
    """A forged user field cannot reach a different tenant's conversations."""

    await seed(service, "session-bob", BOB, tenant="OTHER")

    response = client.get(
        CONVERSATIONS,
        params={"user_id": "bob", "principal_id": "cred-2"},
        headers=auth(ALICE),
    )

    assert response.status_code == 200
    assert _sessions(response.json()) == []


def test_listing_requires_a_bearer_token(client: TestClient) -> None:
    response = client.get(CONVERSATIONS)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_a_foreign_tenant_is_refused(client: TestClient) -> None:
    response = client.get(
        CONVERSATIONS,
        headers={**auth(tenants=("DEFAULT",)), "X-Tenant-Id": "OTHER"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize("limit", [0, 101])
def test_the_page_limit_is_bounded(client: TestClient, limit: int) -> None:
    response = client.get(CONVERSATIONS, params={"limit": limit}, headers=auth())

    assert response.status_code == 422
