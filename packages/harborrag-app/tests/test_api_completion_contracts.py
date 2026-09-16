"""Chat/agent mode validation and the canonical resumable completion contract."""

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.chat.schemas import ChatCitation, ChatMessageResponse
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


@pytest.mark.parametrize("mode", ["rag", "agent"])
@pytest.mark.parametrize("stream", [False, True])
def test_mode_uses_its_own_model_validator_before_creating_or_streaming(
    client, service, monkeypatch, mode, stream
):
    calls = []

    async def reject(model, *, tenant_id):
        calls.append((model, tenant_id))
        raise HarborValidationError("Model unavailable for this mode")

    method = "validate_agent_model" if mode == "agent" else "validate_chat_model"
    monkeypatch.setattr(service, method, reject)
    response = client.post(
        f"/v1/{'agent' if mode == 'agent' else 'chat'}/completions",
        json={"prompt": "Hello", "mode": mode, "stream": stream},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert calls == [(None, "DEFAULT")]
    assert not service.agent_calls and not service.chat_calls
    assert client.get("/v1/chat/sessions").json()["sessions"] == []


@pytest.mark.parametrize("path", ["/v1/agent/runs/run-1/resume"])
def test_resume_preserves_shared_completion_fields_and_prevents_caching(client, service, path):
    session = client.post("/v1/chat/sessions", json={}).json()["session_id"]
    started = client.post(
        "/v1/agent/completions",
        json={"prompt": "Hello", "mode": "agent", "session_id": session},
    )
    resumed = client.post(path, json={"session_id": session})
    assert started.status_code == resumed.status_code == 200
    assert resumed.json() == started.json()
    assert resumed.json()["mode"] == "agent"
    assert resumed.json()["citation_validation"]["complete"] is True
    assert resumed.headers["cache-control"] == "no-store"
    assert "deprecation" not in resumed.headers
    assert len(service.agent_resume_calls) == 1


@pytest.mark.parametrize("extra", [{"prompt": "new"}, {"model": "other"}, {"stream": True}])
def test_canonical_resume_rejects_new_prompt_model_and_stream(client, service, extra):
    response = client.post("/v1/agent/runs/run-1/resume", json={"session_id": "session-1", **extra})
    assert response.status_code == 422
    assert not service.agent_resume_calls


def test_openapi_explains_modes_header_and_canonical_resume(client):
    schema = client.app.openapi()
    completion = schema["paths"]["/v1/chat/completions"]["post"]
    assert "/v1/agent/completions" in completion["description"]
    key = next(p for p in completion["parameters"] if p["name"] == "Idempotency-Key")
    assert key["in"] == "header" and not key["required"]
    resume = schema["paths"]["/v1/agent/runs/{run_id}/resume"]["post"]
    assert not resume.get("deprecated", False)
    assert "409" in resume["responses"]
    agent = schema["components"]["schemas"]["AgentCompletionResponse"]
    assert {"mode", "citations", "citation_validation", "cost"} <= agent["properties"].keys()
    assert {"run_id", "stop_reason", "turns", "tool_calls"} <= set(agent["required"])


@pytest.mark.parametrize(
    "path,mode",
    [
        ("/v1/chat/completions", "rag"),
        ("/v1/agent/completions", "agent"),
        ("/v1/agent/completions", None),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_scope_rejection_precedes_json_or_sse_generation(client, monkeypatch, path, mode, stream):
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
    response = client.post(path, json=body)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["details"] == {"reason": "out_of_scope"}
    assert not service.chat_calls and not service.agent_calls


def test_replay_does_not_pay_for_scope_classification_again(client, service):
    body = {"prompt": "What is the release policy?", "idempotency_key": "scope-replay"}
    assert client.post("/v1/chat/completions", json=body).status_code == 200
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
    citation = ChatCitation(document_id="doc", chunk_id="chunk", score=None, content=content)
    message = ChatMessageResponse(role="assistant", content=content)
    assert citation.content == message.content == content
