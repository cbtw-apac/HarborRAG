"""Story 5 AC1-AC4: chat sessions and history on the explorer contract."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from app_test_conversations import ALICE, BOB, CONVERSATIONS, auth, hmac_app, identity, message
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.workflow_control.memory import CitationRecord, ExchangeRecord, record_exchange

pytestmark = pytest.mark.blackbox

SESSION_FIELDS = {
    "session_id",
    "title",
    "project_id",
    "profile_id",
    "message_count",
    "created_at",
    "updated_at",
}
MESSAGE_FIELDS = {
    "message_id",
    "session_id",
    "role",
    "content",
    "mode",
    "citations",
    "trace_id",
    "feedback",
    "created_at",
}
CITATION = CitationRecord(
    n=1,
    chunk_id="chk_9f31",
    document_id="doc_4411",
    title="CPM-4411 · Senior Go engineer, Hanoi",
    snippet="Five years of Go and Kubernetes.",
    url="https://example.test/browse/CPM-4411",
    page=None,
)


@pytest.fixture
def service() -> MockAppService:
    double = MockAppService()
    double.tenant_projects = {"DEFAULT": {"cpm"}, "GLOBEX": {"globex-hr"}}
    double.catalog.add("cpm", "cpm/cpm-tickets", ("scope-cpm-tickets",), documents=1)
    return double


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    with TestClient(hmac_app(monkeypatch, service)) as test_client:
        yield test_client


def _create(client: TestClient, body: dict[str, object], caller: tuple[str, str] = ALICE) -> str:
    response = client.post(CONVERSATIONS, json=body, headers=auth(caller))
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


async def _two_turns(service: MockAppService, session_id: str) -> tuple[str, str]:
    owner = identity(session_id)
    first = await record_exchange(
        service.conversations,
        owner,
        ExchangeRecord(
            question="Who knows Go?",
            answer="CPM-4411 [1].",
            mode="classic",
            trace_id="0af7651916cd43dd8448eb211c80319c",
            citations=(CITATION,),
        ),
    )
    second = await record_exchange(
        service.conversations,
        owner,
        ExchangeRecord(
            question="And Kubernetes in Hanoi?",
            answer="Two candidates match: CPM-4411 [1].",
            mode="instant",
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            citations=(CITATION,),
        ),
    )
    return first[1].message_id, second[1].message_id


def test_an_empty_body_creates_an_unscoped_session(client: TestClient) -> None:
    response = client.post(CONVERSATIONS, json={}, headers=auth())

    assert response.status_code == 201
    session = response.json()
    assert set(session) == SESSION_FIELDS
    assert session["session_id"].startswith("ses_")
    assert session["project_id"] is None
    assert session["profile_id"] is None
    assert session["title"] == ""
    assert session["message_count"] == 0
    assert response.headers["cache-control"] == "no-store"


def test_a_titled_project_session_answers_the_explorer_session(client: TestClient) -> None:
    response = client.post(
        CONVERSATIONS,
        json={"title": "Go candidates in Hanoi", "project_id": "cpm"},
        headers=auth(),
    )

    assert response.status_code == 201
    session = response.json()
    assert set(session) == SESSION_FIELDS
    assert session["session_id"].startswith("ses_")
    assert session["title"] == "Go candidates in Hanoi"
    assert session["project_id"] == "cpm"
    assert session["profile_id"] is None
    assert session["message_count"] == 0
    created = datetime.fromisoformat(session["created_at"])
    assert created.utcoffset() is not None
    assert session["updated_at"] == session["created_at"]


@pytest.mark.parametrize(
    ("caller_tenants", "project_id"),
    [(("DEFAULT",), "nope"), (("DEFAULT",), "globex-hr")],
    ids=["unknown", "other-tenant"],
)
def test_a_project_outside_the_callers_tenant_is_a_reference_not_found(
    client: TestClient, caller_tenants: tuple[str, ...], project_id: str
) -> None:
    response = client.post(
        CONVERSATIONS,
        json={"project_id": project_id},
        headers=auth(tenants=caller_tenants),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reference_not_found"
    assert response.json()["error"]["details"] == {"field": "project_id"}
    assert client.get(CONVERSATIONS, headers=auth()).json()["items"] == []


def test_a_profile_is_refused_until_v2_1(client: TestClient) -> None:
    response = client.post(CONVERSATIONS, json={"profile_id": "hr-assistant"}, headers=auth())

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [detail["field"] for detail in error["details"]] == ["profile_id"]


@pytest.mark.parametrize("retired", [{"tenant_id": "DEFAULT"}, {"greeting": "Hi"}])
def test_retired_session_fields_are_refused(client: TestClient, retired: dict[str, object]) -> None:
    response = client.post(CONVERSATIONS, json=retired, headers=auth())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_history_after_two_turns_is_newest_first_with_citations(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _create(client, {"title": "Go candidates in Hanoi", "project_id": "cpm"})
    _, newest_answer = await _two_turns(service, session_id)

    response = client.get(f"{CONVERSATIONS}/{session_id}/messages", headers=auth())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = response.json()
    assert page["next_cursor"] is None
    items = page["items"]
    assert [item["role"] for item in items] == ["assistant", "user", "assistant", "user"]
    assert all(set(item) == MESSAGE_FIELDS for item in items)
    newest = items[0]
    assert newest["message_id"] == newest_answer
    assert newest["message_id"].startswith("msg_")
    assert newest["session_id"] == session_id
    assert newest["content"] == "Two candidates match: CPM-4411 [1]."
    assert newest["mode"] == "instant"
    assert newest["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert newest["feedback"] is None
    assert newest["citations"] == [
        {
            "n": 1,
            "chunk_id": "chk_9f31",
            "document_id": "doc_4411",
            "title": "CPM-4411 · Senior Go engineer, Hanoi",
            "snippet": "Five years of Go and Kubernetes.",
            "url": "https://example.test/browse/CPM-4411",
            "page": None,
        }
    ]
    assert items[2]["mode"] == "classic"
    for user in (items[1], items[3]):
        assert user["mode"] is None
        assert user["citations"] == []
        assert user["trace_id"] is None
    listed = client.get(CONVERSATIONS, headers=auth()).json()["items"]
    assert listed[0]["session_id"] == session_id
    assert listed[0]["message_count"] == 4


@pytest.mark.asyncio
async def test_history_pages_walk_every_message_once_newest_first(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _create(client, {})
    await _two_turns(service, session_id)
    path = f"{CONVERSATIONS}/{session_id}/messages"

    first = client.get(path, params={"limit": 3}, headers=auth()).json()
    second = client.get(
        path, params={"limit": 3, "cursor": first["next_cursor"]}, headers=auth()
    ).json()
    whole = client.get(path, headers=auth()).json()

    assert len(first["items"]) == 3
    assert first["next_cursor"]
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert [item["message_id"] for item in first["items"] + second["items"]] == [
        item["message_id"] for item in whole["items"]
    ]


@pytest.mark.parametrize("limit", [0, 101])
def test_the_history_page_limit_is_bounded(client: TestClient, limit: int) -> None:
    session_id = _create(client, {})

    response = client.get(
        f"{CONVERSATIONS}/{session_id}/messages", params={"limit": limit}, headers=auth()
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_stored_session_from_before_the_contract_stays_readable(
    client: TestClient, service: MockAppService
) -> None:
    legacy = identity("session-0b9c1f2e3d4a5b6c7d8e9f0a1b2c3d4e")
    await service.conversations.create(legacy)
    await service.conversations.append_messages(
        legacy,
        (
            message("msg-1", "user", "Where is the runbook?"),
            message(
                "msg-2",
                "assistant",
                'See [Source 1: "Runbook" — Setup].',
                citations_json=(
                    '[{"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9,'
                    ' "document_title": "Runbook", "section_path": ["Setup"]}]'
                ),
            ),
        ),
    )

    response = client.get(f"{CONVERSATIONS}/{legacy.session_id}/messages", headers=auth())

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["message_id"] for item in items] == ["msg-2", "msg-1"]
    assert items[0]["session_id"] == "session-0b9c1f2e3d4a5b6c7d8e9f0a1b2c3d4e"
    assert items[0]["mode"] is None
    assert items[0]["trace_id"] is None
    assert items[0]["citations"] == [
        {
            "n": 1,
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "title": "Runbook",
            "snippet": "",
            "url": None,
            "page": None,
        }
    ]
    listed = client.get(CONVERSATIONS, headers=auth()).json()["items"]
    assert [row["session_id"] for row in listed] == [legacy.session_id]
    assert listed[0]["title"] == ""
    assert listed[0]["project_id"] is None


def test_another_readers_session_is_not_found(client: TestClient) -> None:
    session_id = _create(client, {"title": "Alice only"})

    response = client.get(f"{CONVERSATIONS}/{session_id}/messages", headers=auth(BOB))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_an_unknown_session_is_not_found(client: TestClient) -> None:
    response = client.get(f"{CONVERSATIONS}/ses_nope/messages", headers=auth())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_list_answers_items_of_sessions_with_a_null_last_cursor(client: TestClient) -> None:
    session_id = _create(client, {"title": "Go candidates in Hanoi", "project_id": "cpm"})

    response = client.get(CONVERSATIONS, headers=auth())

    assert response.status_code == 200
    page = response.json()
    assert set(page) == {"items", "next_cursor"}
    assert page["next_cursor"] is None
    assert [set(item) for item in page["items"]] == [SESSION_FIELDS]
    assert page["items"][0]["session_id"] == session_id
    assert page["items"][0]["project_id"] == "cpm"


def test_the_list_is_newest_first_and_only_the_callers_own(client: TestClient) -> None:
    older = _create(client, {"title": "First"})
    newer = _create(client, {"title": "Second"})
    _create(client, {"title": "Bob's"}, BOB)

    items = client.get(CONVERSATIONS, headers=auth()).json()["items"]

    assert [item["session_id"] for item in items] == [newer, older]


def test_the_list_searches_titles(client: TestClient) -> None:
    hanoi = _create(client, {"title": "Go candidates in Hanoi"})
    _create(client, {"title": "Rust contractors"})
    _create(client, {"title": "100%_literal"})

    matched = client.get(CONVERSATIONS, params={"q": "hanoi"}, headers=auth()).json()
    literal = client.get(CONVERSATIONS, params={"q": "0%_"}, headers=auth()).json()
    wildcard = client.get(CONVERSATIONS, params={"q": "%"}, headers=auth()).json()

    assert [item["session_id"] for item in matched["items"]] == [hanoi]
    assert [item["title"] for item in literal["items"]] == ["100%_literal"]
    assert [item["title"] for item in wildcard["items"]] == ["100%_literal"]


def test_the_list_pages_with_a_cursor(client: TestClient) -> None:
    created = [_create(client, {"title": f"Session {index}"}) for index in range(3)]

    first = client.get(CONVERSATIONS, params={"limit": 2}, headers=auth()).json()
    second = client.get(
        CONVERSATIONS, params={"limit": 2, "cursor": first["next_cursor"]}, headers=auth()
    ).json()

    assert first["next_cursor"]
    assert second["next_cursor"] is None
    listed = [item["session_id"] for item in first["items"] + second["items"]]
    assert listed == list(reversed(created))


def test_a_session_from_the_new_route_still_answers_completions(client: TestClient) -> None:
    session_id = _create(client, {"project_id": "cpm"})

    response = client.post(
        "/v1/chat/completions",
        json={"session_id": session_id, "message": "Who knows Go?"},
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.json()["session_id"] == session_id
