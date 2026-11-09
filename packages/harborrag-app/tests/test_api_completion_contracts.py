"""Chat completion documentation, scope refusals and verbatim answer text."""

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_chat_stream import _sse_frames

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.chat.schemas import ChatCompletionResponse

PATH = "/v1/chat/completions"


@pytest.fixture
def service():
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service):
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        yield client


def test_openapi_documents_the_idempotency_header(client):
    completion = client.app.openapi()["paths"][PATH]["post"]
    key = next(p for p in completion["parameters"] if p["name"] == "Idempotency-Key")
    assert key["in"] == "header" and not key["required"]


def test_openapi_documents_both_media_types_and_the_explorer_response(client):
    completion = client.app.openapi()["paths"][PATH]["post"]
    content = completion["responses"]["200"]["content"]
    response = client.app.openapi()["components"]["schemas"]["ChatCompletionResponse"]

    assert {"application/json", "text/event-stream"} <= set(content)
    assert set(response["required"]) == {
        "completion_id",
        "session_id",
        "trace_id",
        "mode",
        "answer",
        "citations",
        "usage",
    }


@pytest.mark.parametrize("stream", [False, True])
def test_an_out_of_scope_request_is_an_ordinary_answer_without_citations(client, service, stream):
    service.chat.scope = "unsupported"
    session = client.post("/v1/chat/sessions", json={}).json()["session_id"]

    response = client.post(
        PATH,
        json={
            "message": "Give me Python code for a snake game",
            "session_id": session,
            "stream": stream,
        },
    )

    assert response.status_code == 200
    payload = _sse_frames(response.text)[-1][1] if stream else response.json()
    public = ChatCompletionResponse.model_validate(payload)
    assert public.answer.startswith("This assistant answers questions about indexed knowledge")
    assert public.session_id == session
    assert public.citations == []
    assert (public.usage.prompt_tokens, public.usage.completion_tokens) == (2, 1)
    assert len(service.chat.gate_requests) == 1
    assert not service.chat.requests


def test_a_refusal_replays_without_checking_the_scope_again(client, service):
    service.chat.scope = "unsupported"
    key = {"Idempotency-Key": "refusal-replay"}

    first = client.post(PATH, json={"message": "Unrelated request"}, headers=key)
    replay = client.post(PATH, json={"message": "Unrelated request"}, headers=key)
    streamed = client.post(PATH, json={"message": "Unrelated request", "stream": True}, headers=key)

    assert first.status_code == replay.status_code == streamed.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert streamed.headers["idempotency-replayed"] == "true"
    assert replay.content == first.content
    assert _sse_frames(streamed.text)[-1][1] == first.json()
    assert len(service.chat.gate_requests) == 1


def test_the_answer_keeps_its_indentation_and_whitespace(client, service):
    service.chat.answer = "  indented source\n    next line\n"

    response = client.post(PATH, json={"message": "Show the code"})

    assert response.json()["answer"] == "  indented source\n    next line\n"


def test_a_completion_body_is_its_explorer_shape_only() -> None:
    assert set(ChatCompletionResponse.model_fields) == {
        "completion_id",
        "session_id",
        "trace_id",
        "mode",
        "answer",
        "citations",
        "usage",
    }
