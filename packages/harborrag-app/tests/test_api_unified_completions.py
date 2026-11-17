"""Completion delivery as JSON or SSE, duplicate suppression, and the conversation routes."""

from __future__ import annotations

import json

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_chat_stream import _sse_frames

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.chat.replay import CompletionAttempt, CompletionInProgressError
from harborrag_app.api.v1.chat.schemas import ChatCompletionRequest
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.models.errors import HarborChatConnectionError

PATH = "/v1/chat/completions"


@pytest.fixture
def service() -> MockAppService:
    double = MockAppService()
    double.known_projects = {"cpm", "fresh"}
    double.catalog.add("cpm", "cpm/tickets", ("scope-tickets",), documents=3)
    return double


@pytest.fixture
def client(monkeypatch, service: MockAppService):
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        yield client


def test_json_and_sse_carry_the_same_completion(client, service):
    session = client.post("/v1/chat/sessions", json={}).json()["session_id"]
    body = {"session_id": session, "message": "Explain the release", "mode": "instant"}

    ordinary = client.post(PATH, json=body, headers={"Idempotency-Key": "json-1"})
    streamed = client.post(PATH, json={**body, "stream": True}, headers={"Idempotency-Key": "s-1"})

    assert ordinary.status_code == streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(streamed.text)
    assert [name for name, _ in frames] == [
        "response.started",
        "response.output_text.delta",
        "response.completed",
    ]
    assert frames[0][1] == {"session_id": session, "mode": "instant", "replayed": False}
    completed = frames[-1][1]
    assert frames[1][1] == {"content": completed["answer"]}
    assert set(completed) == set(ordinary.json())
    assert completed["answer"] == ordinary.json()["answer"]
    assert completed["completion_id"] != ordinary.json()["completion_id"]
    assert len(service.chat.requests) == 2


def test_a_one_off_retry_replays_without_a_session_or_a_model_call(client, service):
    body = {"message": "Explain the release"}
    key = {"Idempotency-Key": "request-1"}

    first = client.post(PATH, json=body, headers=key)
    again = client.post(PATH, json=body, headers=key)
    stream = client.post(PATH, json={**body, "stream": True}, headers=key)

    assert first.status_code == again.status_code == stream.status_code == 200
    assert first.content == again.content
    assert again.headers["idempotency-replayed"] == "true"
    frames = _sse_frames(stream.text)
    assert frames[0][1]["replayed"] is True
    assert frames[-1] == ("response.completed", first.json())
    assert len(service.chat.requests) == 1
    assert client.get("/v1/chat/sessions").json()["items"] == []


def test_a_streamed_result_can_be_retried_as_json(client, service):
    body = {"message": "Explain the release"}
    key = {"Idempotency-Key": "stream-1"}

    streamed = client.post(PATH, json={**body, "stream": True}, headers=key)
    retried = client.post(PATH, json=body, headers=key)

    assert _sse_frames(streamed.text)[-1][1] == retried.json()
    assert retried.headers["idempotency-replayed"] == "true"
    assert len(service.chat.requests) == 1


def test_a_request_that_failed_at_the_model_needs_a_new_key(client, service):
    service.chat.failure = HarborChatConnectionError("provider unreachable")
    key = {"Idempotency-Key": "failed-1"}

    failed = client.post(PATH, json={"message": "Explain"}, headers=key)
    service.chat.failure = None
    retried = client.post(PATH, json={"message": "Explain"}, headers=key)

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "llm_unavailable"
    assert retried.status_code == 409
    assert retried.json()["error"]["code"] == "invalid_state"


def test_a_refusal_before_any_model_call_leaves_the_key_reusable(client, service):
    body = {"message": "Explain the release", "project_ids": ["fresh"]}
    key = {"Idempotency-Key": "prespend-1"}

    refused = client.post(PATH, json=body, headers=key)
    service.catalog.add("fresh", "fresh/wiki", ("scope-wiki",), documents=1)
    retried = client.post(PATH, json=body, headers=key)

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "no_indexed_content"
    assert retried.status_code == 200
    assert retried.headers["idempotency-replayed"] == "false"
    assert len(service.chat.requests) == 1


class _RetrievalOutage:
    """Retrieval that is down for its first ``outages`` searches."""

    def __init__(self, healthy: object, outages: int = 1) -> None:
        self.healthy = healthy
        self.outages = outages

    async def search(self, request: object) -> object:
        if self.outages:
            self.outages -= 1
            raise HarborUnavailableError("the vector store is unavailable")
        return await self.healthy.search(request)  # type: ignore[attr-defined]


class OperationalError(Exception):
    """Stands in for a lost database connection raised by SQLAlchemy."""

    __module__ = "sqlalchemy.exc"


def test_a_retrieval_outage_before_any_model_call_leaves_the_key_reusable(client, service):
    service.chat_runtime.retrieval = _RetrievalOutage(service.chat_runtime.retrieval)
    key = {"Idempotency-Key": "outage-1"}

    failed = client.post(PATH, json={"message": "Explain"}, headers=key)
    retried = client.post(PATH, json={"message": "Explain"}, headers=key)

    assert failed.status_code == 503
    assert retried.status_code == 200
    assert retried.headers["idempotency-replayed"] == "false"
    assert len(service.chat.requests) == 1


def test_a_store_outage_before_any_model_call_names_postgres_and_frees_the_key(
    service, monkeypatch
):
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings()), raise_server_exceptions=False) as client:
        _store_outage_frees_the_key(client, service, monkeypatch)


def _store_outage_frees_the_key(client, service, monkeypatch):
    session = client.post("/v1/chat/sessions", json={}).json()["session_id"]
    original = service.conversations.get_conversation
    calls = {"count": 0}

    async def flaky(identity):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OperationalError("connection refused")
        return await original(identity)

    monkeypatch.setattr(service.conversations, "get_conversation", flaky)
    body = {"session_id": session, "message": "Explain"}
    key = {"Idempotency-Key": "store-1"}

    failed = client.post(PATH, json=body, headers=key)
    retried = client.post(PATH, json=body, headers=key)

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "dependency_unavailable"
    assert failed.json()["error"]["details"] == {"dependency": "postgres"}
    assert retried.status_code == 200
    assert retried.headers["idempotency-replayed"] == "false"


def test_a_store_outage_reading_the_scope_checks_history_frees_the_key(service, monkeypatch):
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings()), raise_server_exceptions=False) as client:
        session = client.post("/v1/chat/sessions", json={}).json()["session_id"]
        original = service.conversations.recent
        calls = {"count": 0}

        async def flaky(identity, *, limit):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OperationalError("connection refused")
            return await original(identity, limit=limit)

        monkeypatch.setattr(service.conversations, "recent", flaky)
        body = {"session_id": session, "message": "Explain", "mode": "classic"}
        key = {"Idempotency-Key": "history-1"}

        failed = client.post(PATH, json=body, headers=key)
        retried = client.post(PATH, json=body, headers=key)

    assert failed.status_code == 503
    assert failed.json()["error"]["details"] == {"dependency": "postgres"}
    assert len(service.chat.gate_requests) == 1
    assert retried.status_code == 200
    assert retried.headers["idempotency-replayed"] == "false"


def test_a_store_failure_after_the_answer_spends_the_key(client, service, monkeypatch):
    session = client.post("/v1/chat/sessions", json={}).json()["session_id"]

    async def refuse(identity, messages):
        raise OperationalError("connection refused")

    monkeypatch.setattr(service.conversations, "append_messages", refuse)
    body = {"session_id": session, "message": "Explain"}
    key = {"Idempotency-Key": "spent-1"}

    failed = client.post(PATH, json=body, headers=key)
    monkeypatch.undo()
    retried = client.post(PATH, json=body, headers=key)

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "dependency_unavailable"
    assert failed.json()["error"]["details"] == {"dependency": "postgres"}
    assert retried.status_code == 409
    assert retried.json()["error"]["code"] == "invalid_state"
    assert len(service.chat.requests) == 1


def test_a_result_stored_by_an_older_release_is_not_replayed(client, service):
    body = {"message": "Explain the release"}
    request = ChatCompletionRequest.model_validate(body)
    attempt = CompletionAttempt.for_request(
        service,
        request,
        tenant_id="DEFAULT",
        principal=Principal("dev", "owner", frozenset({"*"})),
        key="old-1",
    )
    old = {"completion_id": "chat-1", "message": {"role": "assistant", "content": "Answer"}}

    async def store_old_result() -> None:
        assert await attempt.claim() is None
        await attempt.finish(json.dumps(old), session_id=None)

    client.portal.call(store_old_result)
    replayed = client.post(PATH, json=body, headers={"Idempotency-Key": "old-1"})

    assert replayed.status_code == 409
    assert replayed.json()["error"]["code"] == "invalid_state"
    assert not service.chat.requests


@pytest.mark.asyncio
async def test_a_duplicate_while_the_first_runs_is_session_busy() -> None:
    service = MockAppService()
    request = ChatCompletionRequest.model_validate({"message": "One paid call"})
    principal = Principal("dev", "owner", frozenset({"*"}))

    def attempt() -> CompletionAttempt:
        return CompletionAttempt.for_request(
            service, request, tenant_id="DEFAULT", principal=principal, key="concurrent-1"
        )

    assert await attempt().claim() is None
    with pytest.raises(CompletionInProgressError) as busy:
        await attempt().claim()

    assert busy.value.error_code == "session_busy"


def test_client_user_identity_is_rejected(client):
    assert client.post("/v1/chat/sessions", json={"user_id": "other"}).status_code == 422
    assert client.post(PATH, json={"message": "Hi", "user_id": "other"}).status_code == 422


def test_chat_session_routes_are_the_only_conversation_routes(client):
    paths = client.app.openapi()["paths"]
    created = client.post("/v1/chat/sessions", json={})
    assert created.status_code == 201
    assert "deprecation" not in created.headers
    assert not paths[PATH]["post"].get("deprecated", False)
    assert set(paths["/v1/chat/sessions"]) == {"get", "post"}
    assert "/v1/chat/sessions/{session_id}" not in paths
    assert set(paths["/v1/chat/sessions/{session_id}/messages"]) == {"get"}
    for removed in ("/v1/conversations", "/v1/chat/conversations"):
        assert client.get(removed).status_code == 404
        assert client.post(removed, json={}).status_code == 404
        assert removed not in paths
