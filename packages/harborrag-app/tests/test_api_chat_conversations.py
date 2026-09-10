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
    rows = payload["conversations"]
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
    rows = response.json()["conversations"]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "session-1"
    assert rows[0]["kind"] == "chat"
    assert rows[0]["title"] == "Runbooks"
    assert rows[0]["message_count"] == 1
    assert rows[0]["created_at"] and rows[0]["updated_at"]
    # No owner field is echoed back: the caller already is the owner.
    assert "user_id" not in rows[0]
    assert "next_cursor" not in response.json()


@pytest.mark.asyncio
async def test_a_session_created_over_http_is_listed_with_its_title(
    client: TestClient,
) -> None:
    created = client.post(
        "/v1/chat/sessions",
        json={"tenant": "DEFAULT", "title": "  Quarterly report  "},
        headers=auth(),
    )
    assert created.status_code == 201

    response = client.get(CONVERSATIONS, headers=auth())

    rows = response.json()["conversations"]
    assert _sessions(response.json()) == [created.json()["session_id"]]
    assert rows[0]["title"] == "Quarterly report"


def test_creating_a_session_without_a_title_is_unchanged(client: TestClient) -> None:
    """Existing callers send no title, and get an untitled conversation."""

    created = client.post("/v1/chat/sessions", json={}, headers=auth())

    assert created.status_code == 201
    rows = client.get(CONVERSATIONS, headers=auth()).json()["conversations"]
    assert "title" not in rows[0]


@pytest.mark.asyncio
async def test_the_kind_filter_separates_chat_from_agent_conversations(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-chat")
    await seed(service, "session-agent", kind="agent")

    chats = client.get(CONVERSATIONS, params={"kind": "chat"}, headers=auth())
    agents = client.get(CONVERSATIONS, params={"kind": "agent"}, headers=auth())

    assert _sessions(chats.json()) == ["session-chat"]
    assert _sessions(agents.json()) == ["session-agent"]


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
    assert "next_cursor" not in second
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
    assert response.json()["error"]["code"] == "harbor_validation_error"


@pytest.mark.asyncio
async def test_a_cursor_for_someone_elses_conversation_is_rejected(
    client: TestClient,
    service: MockAppService,
) -> None:
    """A well-formed cursor naming a foreign row must not page from the start."""

    await seed(service, "session-1")
    await seed(service, "session-bob", BOB)

    response = client.get(
        CONVERSATIONS,
        params={"cursor": encode_conversation_cursor("session-bob")},
        headers=auth(),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_second_user_never_sees_the_first_users_conversations(
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
    """Owner fields come from the token; sending them changes nothing."""

    await seed(service, "session-bob", BOB)

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
    assert response.json()["error"]["code"] == "harbor_auth_error"


def test_a_foreign_tenant_is_refused(client: TestClient) -> None:
    response = client.get(
        CONVERSATIONS,
        params={"tenant": "OTHER"},
        headers=auth(tenants=("DEFAULT",)),
    )

    assert response.status_code == 403


@pytest.mark.parametrize("limit", [0, 101])
def test_the_page_limit_is_bounded(client: TestClient, limit: int) -> None:
    response = client.get(CONVERSATIONS, params={"limit": limit}, headers=auth())

    assert response.status_code == 422
