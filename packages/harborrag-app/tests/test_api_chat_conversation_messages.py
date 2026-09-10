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
    rows = payload["messages"]
    assert isinstance(rows, list)
    return [str(row["message_id"]) for row in rows]


@pytest.mark.asyncio
async def test_messages_come_back_oldest_first(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(
        service,
        "session-1",
        messages=(
            message("msg-1", "user", "where is the runbook?"),
            message("msg-2", "assistant", "in the wiki", token_count=3, run_id="run-1"),
        ),
    )

    response = client.get(_path(), headers=auth())

    assert response.status_code == 200
    payload = response.json()
    assert _ids(payload) == ["msg-1", "msg-2"]
    assert payload["messages"][1] == {
        "message_id": "msg-2",
        "role": "assistant",
        "content": "in the wiki",
        "created_at": payload["messages"][1]["created_at"],
        "token_count": 3,
        "citations": [],
        "run_id": "run-1",
        "partial": False,
    }
    assert "next_cursor" not in payload


@pytest.mark.asyncio
async def test_a_partial_answer_is_marked_and_keeps_its_citations(
    client: TestClient,
    service: MockAppService,
) -> None:
    """A stream that died mid-answer must be readable as incomplete."""

    await seed(
        service,
        "session-1",
        messages=(
            message(
                "msg-1",
                "assistant",
                "the runbook is",
                citations_json='[{"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}]',
                partial=True,
            ),
        ),
    )

    response = client.get(_path(), headers=auth())

    row = response.json()["messages"][0]
    assert row["partial"] is True
    assert row["citations"] == [{"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}]


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
    assert response.json()["messages"][0]["citations"] == []


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
        params={"limit": 2, "after": first["next_cursor"]},
        headers=auth(),
    ).json()

    assert _ids(first) == ["msg-0", "msg-1"]
    assert first["next_cursor"] == "msg-1"
    assert _ids(second) == ["msg-2"]
    assert "next_cursor" not in second


@pytest.mark.asyncio
async def test_a_full_last_page_does_not_advertise_an_empty_next_page(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", messages=(message("msg-1"), message("msg-2")))

    payload = client.get(_path(), params={"limit": 2}, headers=auth()).json()

    assert _ids(payload) == ["msg-1", "msg-2"]
    assert "next_cursor" not in payload


@pytest.mark.asyncio
async def test_an_unknown_message_cursor_is_a_client_error(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", messages=(message("msg-1"),))

    response = client.get(_path(), params={"after": "msg-absent"}, headers=auth())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_an_unknown_conversation_is_the_not_found_envelope(client: TestClient) -> None:
    response = client.get(_path("session-absent"), headers=auth())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.asyncio
async def test_a_second_user_cannot_read_the_first_users_messages(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", ALICE, messages=(message("msg-1", "user", "private"),))

    response = client.get(_path(), headers=auth(BOB))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.asyncio
async def test_reading_messages_requires_a_bearer_token(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", messages=(message("msg-1"),))

    response = client.get(_path())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "harbor_auth_error"


@pytest.mark.asyncio
async def test_a_foreign_tenant_is_refused(
    client: TestClient,
    service: MockAppService,
) -> None:
    await seed(service, "session-1", messages=(message("msg-1"),))

    response = client.get(_path(), params={"tenant": "OTHER"}, headers=auth())

    assert response.status_code == 403


@pytest.mark.parametrize("limit", [0, 201])
def test_the_message_page_limit_is_bounded(client: TestClient, limit: int) -> None:
    response = client.get(_path(), params={"limit": limit}, headers=auth())

    assert response.status_code == 422
