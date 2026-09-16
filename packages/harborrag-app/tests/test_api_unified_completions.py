"""Unified completion delivery, duplicate suppression, and conversation migration."""

from __future__ import annotations

import asyncio

import pytest
from app_test_fixtures import MockAppService
from fastapi import Response
from fastapi.testclient import TestClient
from test_api_chat_stream import _sse_frames

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.chat.routes import _complete_chat
from harborrag_app.api.v1.chat.schemas import ChatCompletionRequest
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborConflictError


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService):
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        yield client


@pytest.mark.parametrize("mode", ["rag", "agent"])
def test_modes_share_session_and_final_json_sse_contract(client, service, mode):
    session = client.post("/v1/conversations", json={}).json()["session_id"]
    body = {"session_id": session, "prompt": "Explain the release", "mode": mode}
    ordinary = client.post("/v1/chat/completions", json=body)
    streamed = client.post("/v1/chat/completions", json={**body, "stream": True})
    assert ordinary.status_code == streamed.status_code == 200
    frames = _sse_frames(streamed.text)
    assert frames[0] == (
        "response.started",
        {"session_id": session, "mode": mode, "replayed": False},
    )
    assert frames[-1] == ("response.completed", ordinary.json())
    assert ordinary.json()["cost"]["amount_usd"] is None
    assert ordinary.json()["cost"]["status"] == "unavailable"
    assert ordinary.json()["mode"] == mode
    calls = service.chat_calls if mode == "rag" else service.agent_calls
    assert calls[0]["user_id"] == "DEFAULT_USER"


@pytest.mark.parametrize("mode", ["rag", "agent"])
def test_retry_without_session_replays_without_new_session_or_model_call(client, service, mode):
    body = {"prompt": "Explain the release", "mode": mode, "idempotency_key": "request-1"}
    first = client.post("/v1/chat/completions", json=body)
    again = client.post("/v1/chat/completions", json=body)
    stream = client.post("/v1/chat/completions", json={**body, "stream": True})
    assert first.status_code == again.status_code == stream.status_code == 200
    assert first.json() == again.json()
    assert again.headers["idempotency-replayed"] == "true"
    frames = _sse_frames(stream.text)
    assert frames[0][1]["replayed"] is True
    assert frames[-1] == ("response.completed", first.json())
    calls = service.chat_calls if mode == "rag" else service.agent_calls
    assert len(calls) == 1
    assert len(client.get("/v1/conversations").json()["conversations"]) == 1


def test_retry_echoing_the_disclosed_session_replays(client, service):
    """The session the first attempt handed back does not change the request.

    A stream announces ``session_id`` in its first frame, so the natural retry
    after a mid-stream disconnect carries a session the original request did
    not. That is the same paid operation and must replay, not conflict.
    """

    body = {"prompt": "Explain the release", "idempotency_key": "disclosed-1"}
    first = client.post("/v1/chat/completions", json={**body, "stream": True})
    session = _sse_frames(first.text)[0][1]["session_id"]
    retried = client.post("/v1/chat/completions", json={**body, "session_id": session})
    assert retried.status_code == 200
    assert retried.headers["idempotency-replayed"] == "true"
    assert retried.json() == _sse_frames(first.text)[-1][1]
    assert len(service.chat_calls) == 1


def test_stream_result_can_be_retried_as_json(client, service):
    body = {"prompt": "Explain the release", "idempotency_key": "stream-1"}
    streamed = client.post("/v1/chat/completions", json={**body, "stream": True})
    retried = client.post("/v1/chat/completions", json=body)
    assert _sse_frames(streamed.text)[-1][1] == retried.json()
    assert len(service.chat_calls) == 1


def test_reusing_a_key_for_a_different_prompt_is_conflict(client, service):
    headers = {"Idempotency-Key": "same-key"}
    assert (
        client.post("/v1/chat/completions", json={"prompt": "one"}, headers=headers).status_code
        == 200
    )
    conflict = client.post("/v1/chat/completions", json={"prompt": "two"}, headers=headers)
    assert conflict.status_code == 409
    assert len(service.chat_calls) == 1


def test_failed_request_requires_an_explicit_new_key(client, service, monkeypatch):
    async def failed(*args, **kwargs):
        return AppResponse(False, {}, "Unavailable")

    monkeypatch.setattr(service, "chat_completion", failed)
    body = {"prompt": "Explain the release", "idempotency_key": "failed-1"}
    assert client.post("/v1/chat/completions", json=body).status_code == 503
    assert client.post("/v1/chat/completions", json=body).status_code == 409


def test_failure_before_dispatch_leaves_the_key_reusable(client, service, monkeypatch):
    """A turn that never reached a model must not consume the caller's key.

    ``test_failed_request_requires_an_explicit_new_key`` pins the opposite for a
    failure *after* dispatch, where spend may already have happened. Here the
    session store is what fails, so nothing was charged.
    """

    async def unavailable(*args, **kwargs):
        return AppResponse(False, {}, "Unavailable")

    monkeypatch.setattr(service, "create_chat_session", unavailable)
    body = {"prompt": "Explain the release", "idempotency_key": "prespend-1"}
    assert client.post("/v1/chat/completions", json=body).status_code == 503
    monkeypatch.undo()
    retried = client.post("/v1/chat/completions", json=body)
    assert retried.status_code == 200
    assert retried.headers["idempotency-replayed"] == "false"
    assert len(service.chat_calls) == 1


def test_deleted_conversation_cannot_be_replayed(client, service):
    body = {"prompt": "Private answer", "idempotency_key": "erased-1"}
    first = client.post("/v1/chat/completions", json=body)
    session = first.json()["session_id"]
    assert client.delete(f"/v1/conversations/{session}").status_code == 200
    assert client.post("/v1/chat/completions", json=body).status_code == 409
    assert len(service.chat_calls) == 1


def test_initial_title_and_client_user_identity_are_rejected(client):
    assert client.post("/v1/conversations", json={"title": "Initial"}).status_code == 422
    assert (
        client.post("/v1/chat/completions", json={"prompt": "Hi", "user_id": "other"}).status_code
        == 422
    )


def test_deprecated_routes_expose_successor_and_sunset(client):
    old = client.post("/v1/chat/sessions", json={})
    assert old.headers["deprecation"] == "true"
    assert "Sunset" in old.headers
    assert "/v1/conversations" in old.headers["link"]
    new = client.post("/v1/conversations", json={})
    assert "deprecation" not in new.headers
    paths = client.app.openapi()["paths"]
    assert paths["/v1/agent/completions"]["post"]["deprecated"] is True
    assert not paths["/v1/conversations"]["post"].get("deprecated", False)


@pytest.mark.asyncio
async def test_concurrent_duplicate_is_rejected_while_first_request_runs(service, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    original = service.chat_completion

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "chat_completion", blocked)
    request = ChatCompletionRequest(prompt="One paid call", idempotency_key="concurrent-1")
    principal = Principal("dev", "owner", frozenset({"*"}))

    async def execute():
        return await _complete_chat(request, service, principal, Response(), settings=ApiSettings())

    first = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        with pytest.raises(HarborConflictError, match="already running"):
            await execute()
    finally:
        release.set()
        await first
    assert len(service.chat_calls) == 1
