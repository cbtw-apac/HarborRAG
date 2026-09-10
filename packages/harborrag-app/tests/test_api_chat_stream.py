"""Contract tests for streamed chat completions through the public HTTP API."""

from __future__ import annotations

import asyncio
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


@pytest.fixture
def short_deadline_client(monkeypatch, service: MockAppService) -> TestClient:
    """1s handler deadline, 3s stream deadline: streams outlive the request timeout."""

    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(api_request_timeout_seconds=1.0, api_stream_timeout_seconds=3.0)
    with TestClient(create_fastapi_app(settings)) as test_client:
        yield test_client


def _sse_frames(body: str) -> list[tuple[str, dict[str, object]]]:
    frames = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        frames.append((event, json.loads(data)))
    return frames


def _session(client: TestClient, tenant: str = "DEFAULT") -> str:
    response = client.post("/v1/chat/sessions", json={"tenant": tenant})
    assert response.status_code == 201
    return response.json()["session_id"]


def test_chat_stream_emits_citations_then_deltas_then_completion(
    client: TestClient,
    service: MockAppService,
) -> None:
    session_id = _session(client, "ACME")
    response = client.post(
        "/v1/chat/completions",
        json={
            "tenant": "ACME",
            "prompt": "What is HarborRAG?",
            "graph_search": True,
            "stream": True,
            "session_id": session_id,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(response.text)
    names = [name for name, _ in frames]
    assert names == ["citations", "text_delta", "completed"]
    assert frames[0][1]["citations"] == [
        {"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}
    ]
    assert frames[0][1]["session_id"] == session_id
    assert frames[1][1]["content"] == "Harbor response"
    assert frames[2][1]["finish_reason"] == "stop"

    call = service.chat_calls[0]
    assert call["tenant_id"] == "ACME"
    assert call["system"] == "default"
    assert call["query"] == "What is HarborRAG?"
    assert call["graph_search"] is True


def test_chat_stream_ends_with_error_event_on_failure(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def fail(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        yield {"kind": "error", "error": "provider key and private endpoint"}

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: fail(query))
    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    assert response.status_code == 200
    frames = _sse_frames(response.text)
    assert frames == [
        ("error", {"code": "harbor_connection_error", "message": "Chat service is unavailable"})
    ]
    assert "private endpoint" not in response.text


def test_chat_stream_rejects_empty_prompt(client: TestClient) -> None:
    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "", "session_id": session_id, "stream": True},
    )

    assert response.status_code == 422


def test_chat_stream_outlives_the_request_deadline(
    short_deadline_client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    """The request timeout covers the handler only; a slow body is not cancelled."""

    async def slow(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        for index in range(3):
            await asyncio.sleep(0.5)
            yield {
                "kind": "chunk",
                "chunk": {"event": "text_delta", "content": f"part-{index}"},
            }

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: slow(query))
    session_id = _session(short_deadline_client)
    response = short_deadline_client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    assert response.status_code == 200
    frames = _sse_frames(response.text)
    assert [name for name, _ in frames] == ["text_delta"] * 3


def test_chat_stream_past_its_deadline_ends_with_a_terminal_error_frame(
    short_deadline_client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def stalled(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        yield {"kind": "citations", "citations": (), "session_id": "session"}
        await asyncio.sleep(30)
        yield {"kind": "chunk", "chunk": {"event": "text_delta", "content": "never"}}

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: stalled(query))
    session_id = _session(short_deadline_client)
    response = short_deadline_client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    assert response.status_code == 200
    frames = _sse_frames(response.text)
    assert [name for name, _ in frames] == ["citations", "error"]
    assert frames[-1][1] == {
        "code": "harbor_deadline_exceeded",
        "message": "Chat stream exceeded its server deadline",
    }
    assert response.text.endswith("\n\n")


def test_chat_stream_projects_memory_warning_as_non_terminal_frame(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def events(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        yield {"kind": "chunk", "chunk": {"event": "completed", "finish_reason": "stop"}}
        yield {"kind": "warning", "warning": "conversation_memory_unavailable"}

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: events(query))
    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    frames = _sse_frames(response.text)
    assert [name for name, _ in frames] == ["completed", "warning"]
    assert frames[1][1]["code"] == "conversation_memory_unavailable"


def test_chat_stream_rejects_an_agent_session(client: TestClient) -> None:
    created = client.post("/v1/agent/sessions", json={"tenant": "DEFAULT"})
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": created.json()["session_id"], "stream": True},
    )

    assert response.status_code == 404
