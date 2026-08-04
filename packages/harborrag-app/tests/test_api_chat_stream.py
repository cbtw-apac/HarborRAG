"""Contract tests for streamed chat completions through the public HTTP API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _sse_frames(body: str) -> list[tuple[str, dict[str, object]]]:
    frames = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        frames.append((event, json.loads(data)))
    return frames


def test_chat_stream_emits_citations_then_deltas_then_completion(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/chat/stream",
        json={"tenant": "ACME", "system": "concise", "prompt": "What is HarborRAG?"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(response.text)
    names = [name for name, _ in frames]
    assert names == ["citations", "text_delta", "completed"]
    assert frames[0][1] == {
        "citations": [{"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}]
    }
    assert frames[1][1]["content"] == "Harbor response"
    assert frames[2][1]["finish_reason"] == "stop"

    call = service.chat_calls[0]
    assert call["tenant_id"] == "ACME"
    assert call["system"] == "concise"
    assert call["query"] == "What is HarborRAG?"


def test_chat_stream_ends_with_error_event_on_failure(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def fail(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        yield {"kind": "error", "error": "provider key and private endpoint"}

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: fail(query))
    response = client.post("/v1/chat/stream", json={"prompt": "Hello"})

    assert response.status_code == 200
    frames = _sse_frames(response.text)
    assert frames == [
        ("error", {"code": "harbor_connection_error", "message": "Chat service is unavailable"})
    ]
    assert "private endpoint" not in response.text


def test_chat_stream_rejects_empty_prompt(client: TestClient) -> None:
    response = client.post("/v1/chat/stream", json={"prompt": ""})

    assert response.status_code == 422
