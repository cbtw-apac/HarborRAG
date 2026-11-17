"""``stream: true`` until Task 7: the finished completion sent as SSE frames.

TODO: replace with the Story 5 event stream (``run.started`` ... ``run.completed``) in Task 7.
"""

from __future__ import annotations

import json

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
    response = client.post("/v1/chat/sessions", json={}, headers={"X-Tenant-Id": tenant})
    assert response.status_code == 201
    return response.json()["session_id"]


def test_a_stream_sends_the_answer_then_the_completion_stored_in_history(
    client: TestClient,
) -> None:
    session_id = _session(client)

    response = client.post(
        "/v1/chat/completions",
        json={"session_id": session_id, "message": "Explain HarborRAG", "stream": True},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(response.text)
    assert [name for name, _ in frames] == [
        "response.started",
        "response.output_text.delta",
        "response.completed",
    ]
    completion = frames[-1][1]
    assert frames[0][1] == {"session_id": session_id, "mode": "classic", "replayed": False}
    assert frames[1][1] == {"content": completion["answer"]}
    newest = client.get(f"/v1/chat/sessions/{session_id}/messages").json()["items"][0]
    assert newest["message_id"] == completion["completion_id"]
    assert newest["content"] == completion["answer"]
    assert newest["citations"] == completion["citations"]
    assert newest["trace_id"] == completion["trace_id"]


def test_a_stream_refuses_an_empty_message_before_it_opens(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json={"message": "", "stream": True})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
