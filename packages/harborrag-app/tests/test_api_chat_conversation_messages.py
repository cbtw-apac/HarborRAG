"""Reading one conversation's messages back over the public HTTP surface."""

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


def _path(session_id: str = "session-1") -> str:
    return f"{CONVERSATIONS}/{session_id}/messages"


def _ids(payload: dict[str, object]) -> list[str]:
    rows = payload["items"]
    assert isinstance(rows, list)
    return [str(row["message_id"]) for row in rows]


@pytest.mark.asyncio
async def test_messages_come_back_newest_first(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(
        service,
        "session-1",
        messages=(
            message("msg-1", "user", "where is the runbook?"),
            message("msg-2", "assistant", "in the wiki", token_count=3),
        ),
    )

    response = client.get(_path(), headers=auth())

    assert response.status_code == 200
    payload = response.json()
    assert _ids(payload) == ["msg-2", "msg-1"]
    assert payload["items"][0] == {
        "message_id": "msg-2",
        "session_id": "session-1",
        "role": "assistant",
        "content": "in the wiki",
        "mode": None,
        "citations": [],
        "trace_id": None,
        "feedback": None,
        "created_at": payload["items"][0]["created_at"],
    }
    assert payload["next_cursor"] is None


@pytest.mark.asyncio
async def test_a_partial_answer_is_left_out_of_history(
    client: TestClient,
    service: MockAppService,
) -> None:
    """A stream that died mid-answer must never read as a finished answer."""

    await seed(
        service,
        "session-1",
        messages=(
            message("msg-1", "user", "where is the runbook?"),
            message(
                "msg-2",
                "assistant",
                "the runbook is",
                citations_json='[{"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}]',
                partial=True,
            ),
        ),
    )

    response = client.get(_path(), headers=auth())

    assert _ids(response.json()) == ["msg-1"]
    assert client.get(CONVERSATIONS, headers=auth()).json()["items"][0]["message_count"] == 1


@pytest.mark.asyncio
async def test_unparseable_stored_citations_read_back_as_none(
    client: TestClient,
    service: MockAppService,
) -> None:
    """An older or hand-edited payload must not turn a read into a 500."""

    await seed(
        service,
        "session-1",
        messages=(message("msg-1", "assistant", "answer", citations_json="not json"),),
    )

    response = client.get(_path(), headers=auth())

    assert response.status_code == 200
    assert response.json()["items"][0]["citations"] == []


@pytest.mark.asyncio
async def test_paging_walks_the_history_exactly_once(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(
        service,
        "session-1",
        messages=tuple(message(f"msg-{index}") for index in range(3)),
    )

    first = client.get(_path(), params={"limit": 2}, headers=auth()).json()
    second = client.get(
        _path(),
        params={"limit": 2, "cursor": first["next_cursor"]},
        headers=auth(),
    ).json()

    assert _ids(first) == ["msg-2", "msg-1"]
    assert first["next_cursor"]
    assert _ids(second) == ["msg-0"]
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_a_full_last_page_does_not_advertise_an_empty_next_page(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", messages=(message("msg-1"), message("msg-2")))

    payload = client.get(_path(), params={"limit": 2}, headers=auth()).json()

    assert _ids(payload) == ["msg-2", "msg-1"]
    assert payload["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [encode_conversation_cursor("msg-absent"), "!!!!"])
async def test_an_unknown_or_malformed_cursor_is_a_client_error(
    client: TestClient,
    service: MockAppService,
    cursor: str,
) -> None:
    await seed(service, "session-1", messages=(message("msg-1"),))

    response = client.get(_path(), params={"cursor": cursor}, headers=auth())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"][0]["field"] == "cursor"


def test_an_unknown_conversation_is_the_not_found_envelope(client: TestClient) -> None:
    response = client.get(_path("session-absent"), headers=auth())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_another_user_cannot_read_private_history(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", ALICE, messages=(message("msg-1", "user", "private"),))

    response = client.get(_path(), headers=auth(BOB))

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reading_messages_requires_a_bearer_token(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", messages=(message("msg-1"),))

    response = client.get(_path())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.asyncio
async def test_a_foreign_tenant_is_refused(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", messages=(message("msg-1"),))

    response = client.get(_path(), headers={**auth(), "X-Tenant-Id": "OTHER"})

    assert response.status_code == 403


@pytest.mark.parametrize("limit", [0, 101])
def test_the_message_page_limit_is_bounded(client: TestClient, limit: int) -> None:
    response = client.get(_path(), params={"limit": limit}, headers=auth())

    assert response.status_code == 422
