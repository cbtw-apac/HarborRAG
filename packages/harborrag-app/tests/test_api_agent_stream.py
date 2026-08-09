"""Contract tests for streamed agent runs through the public HTTP API."""

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


def _session(client: TestClient, tenant: str = "DEFAULT") -> str:
    response = client.post("/v1/agent/sessions", json={"tenant": tenant})
    assert response.status_code == 201
    return response.json()["session_id"]


def test_agent_stream_emits_progress_events_then_result(
    client: TestClient,
    service: MockAppService,
) -> None:
    session_id = _session(client, "ACME")
    response = client.post(
        "/v1/agent/completions",
        json={
            "tenant": "ACME",
            "prompt": "Connect the release policy to its owner.",
            "graph_search": True,
            "stream": True,
            "session_id": session_id,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(response.text)
    names = [name for name, _ in frames]
    assert names == ["run.started", "run.completed", "result"]
    assert frames[0][1]["run_id"] == "run-1"
    assert frames[-1][1]["message"]["content"] == "Agent response"
    assert frames[-1][1]["run_id"] == "run-1"

    call = service.agent_calls[0]
    assert call["tenant_id"] == "ACME"
    assert call["graph_search"] is True


def test_agent_stream_ends_with_error_event_on_failure(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    async def fail(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        yield {"kind": "error", "error": "provider key and private endpoint"}

    monkeypatch.setattr(service, "agent_stream", lambda query, **_: fail(query))
    session_id = _session(client)
    response = client.post(
        "/v1/agent/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    assert response.status_code == 200
    frames = _sse_frames(response.text)
    assert frames == [
        ("error", {"code": "harbor_connection_error", "message": "Agent service is unavailable"})
    ]
    assert "private endpoint" not in response.text


def test_agent_stream_rejects_unknown_session(client: TestClient) -> None:
    response = client.post(
        "/v1/agent/completions",
        json={"prompt": "Hello", "session_id": "session-missing", "stream": True},
    )

    assert response.status_code == 404


def test_agent_stream_rejects_empty_prompt(client: TestClient) -> None:
    session_id = _session(client)
    response = client.post(
        "/v1/agent/completions",
        json={"prompt": "", "session_id": session_id, "stream": True},
    )

    assert response.status_code == 422
