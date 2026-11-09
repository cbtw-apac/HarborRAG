"""Chat completion model validation, scope refusals and replay."""

import json

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.chat.replay import _stored_response
from harborrag_app.api.v1.chat.schemas import (
    ChatCitation,
    ChatCompletionResponse,
    ChatMessageResponse,
)
from harborrag_app.api.v1.chat.streaming import progress_frame
from harborrag_core.contracts.errors import HarborValidationError


@pytest.fixture
def service():
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service):
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        yield client


@pytest.mark.parametrize("stream", [False, True])
def test_the_model_is_validated_before_creating_or_streaming(client, service, monkeypatch, stream):
    calls = []

    async def reject(model, *, tenant_id):
        calls.append((model, tenant_id))
        raise HarborValidationError("Model unavailable")

    monkeypatch.setattr(service, "validate_chat_model", reject)
    response = client.post("/v1/chat/completions", json={"prompt": "Hello", "stream": stream})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert calls == [(None, "DEFAULT")]
    assert not service.chat_calls
    assert client.get("/v1/chat/sessions").json()["items"] == []


def test_openapi_documents_the_idempotency_header(client):
    completion = client.app.openapi()["paths"]["/v1/chat/completions"]["post"]
    key = next(p for p in completion["parameters"] if p["name"] == "Idempotency-Key")
    assert key["in"] == "header" and not key["required"]


@pytest.mark.parametrize("mode", ["rag", None])
@pytest.mark.parametrize("stream", [False, True])
def test_scope_refusal_uses_completion_schema_without_answer_generation(
    client, monkeypatch, mode, stream
):
    service = client.app.state.app_service

    async def reject(*args, **kwargs):
        raise HarborValidationError("Outside indexed knowledge", {"reason": "out_of_scope"})

    monkeypatch.setattr(service, "validate_completion_scope", reject)
    session = client.post("/v1/chat/sessions", json={}).json()["session_id"]
    body = {
        "prompt": "Give me Python code for a snake game",
        "session_id": session,
        "stream": stream,
    }
    if mode is not None:
        body["mode"] = mode
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    if stream:
        frames = [
            (name, json.loads(data))
            for block in response.text.strip().split("\n\n")
            for name, data in [
                tuple(line.split(": ", 1)[1] for line in block.splitlines() if ": " in line)
            ]
        ]
        assert [name for name, _ in frames] == ["response.started", "response.completed"]
        assert frames[0][1]["replayed"] is False
        payload = frames[1][1]
    else:
        payload = response.json()
    public = ChatCompletionResponse.model_validate(payload)
    assert public.outcome == "refused"
    assert public.refusal_reason == public.finish_reason == "out_of_scope"
    assert public.message.content == "Outside indexed knowledge"
    assert public.session_id == session
    assert public.citations == ()
    assert public.memory_persisted is False
    assert not service.chat_calls


def test_scope_refusal_creates_session_and_replays_without_reclassifying(
    client, service, monkeypatch
):
    calls = []

    async def reject(*args, **kwargs):
        calls.append(args[0])
        raise HarborValidationError("Outside indexed knowledge", {"reason": "out_of_scope"})

    monkeypatch.setattr(service, "validate_completion_scope", reject)
    body = {"prompt": "Unrelated request", "idempotency_key": "refusal-replay"}
    first = client.post("/v1/chat/completions", json=body)
    replay = client.post("/v1/chat/completions", json={**body, "stream": False})
    streamed_replay = client.post("/v1/chat/completions", json={**body, "stream": True})

    assert first.status_code == replay.status_code == streamed_replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert streamed_replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == first.json()
    completed = streamed_replay.text.split("event: response.completed\ndata: ", 1)[1].split(
        "\n\n", 1
    )[0]
    assert json.loads(completed) == first.json()
    assert first.json()["session_id"]
    assert len(calls) == 1


def test_replay_does_not_pay_for_scope_classification_again(client, service):
    body = {"prompt": "What is the release policy?", "idempotency_key": "scope-replay"}
    answered = client.post("/v1/chat/completions", json=body)
    assert answered.status_code == 200
    assert answered.json()["outcome"] == "answered"
    assert answered.json()["refusal_reason"] is None
    assert client.post("/v1/chat/completions", json={**body, "stream": True}).status_code == 200
    assert len(service.scope_calls) == 1


def test_display_delta_does_not_expose_reasoning_or_provider_fields():
    frame = progress_frame(
        {
            "kind": "chunk",
            "chunk": {
                "event": "text_delta",
                "content": "Visible",
                "reasoning": "Private reasoning",
                "deployment": "Private provider deployment",
            },
        }
    )
    assert frame == b'event: response.output_text.delta\ndata: {"content": "Visible"}\n\n'


def test_response_text_preserves_source_indentation_and_streamed_whitespace():
    content = "  indented source\n    next line\n"
    citation = ChatCitation(document_id="doc", chunk_id="chunk", score=0.0, content=content)
    message = ChatMessageResponse(role="assistant", content=content)
    assert citation.content == message.content == content


def test_a_result_stored_before_the_upgrade_still_replays():
    stored = {
        "id": "answer-1",
        "model": "primary",
        "provider": "mock",
        "provider_model": "mock-chat",
        "message": {"role": "assistant", "content": "Answer"},
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "retry_count": 0,
        "fallback_count": 0,
        "citations": [
            {"document_id": "doc", "chunk_id": "chunk", "score": 0.5, "tool": "vector_search"}
        ],
        "citation_validation": {"complete": True},
        "session_id": "session-1",
        "run_id": "run-1",
        "stop_reason": "final_answer",
        "turns": 2,
        "tool_call_count": 1,
        "tool_calls": [{"step": 1, "tool": "vector_search", "ok": True}],
    }

    replayed = _stored_response(json.dumps(stored))

    assert replayed.completion_id == "answer-1"
    assert replayed.session_id == "session-1"
    assert replayed.citations[0].chunk_id == "chunk"
    assert "run_id" not in replayed.model_dump()
