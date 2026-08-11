"""Contract tests for the ingestion task SSE stream (ML2): replay + live tail."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.events import HarborEvent


class _StreamingAppService(MockAppService):
    """MockAppService plus a test-controllable stream_ingestion_events()."""

    def __init__(self, *, status: str, events: list[HarborEvent]) -> None:
        super().__init__()
        self._status = status
        self._events = events

    async def get_task(self, task_id: str) -> dict[str, object]:
        return {"task_id": task_id, "tenant": "DEFAULT", "status": self._status}

    async def stream_ingestion_events(self, task_id: str) -> AsyncIterator[HarborEvent]:
        del task_id
        for event in self._events:
            yield event


def _sse_frames(body: str) -> list[tuple[str, dict[str, object]]]:
    if not body.strip():
        return []
    frames = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        frames.append((event, json.loads(data)))
    return frames


def _client(service: MockAppService, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    return TestClient(create_fastapi_app(ApiSettings()))


def test_stream_replays_backlog_and_closes_for_an_already_terminal_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal task's stream is pure backlog replay -- no live tail is opened."""
    events = [
        HarborEvent(name="task.t1.progress", trace_id="t1", payload={"n": 1}),
        HarborEvent(name="task.t1.done", trace_id="t1", payload={"n": 2}),
    ]
    service = _StreamingAppService(status="SUCCESS", events=events)
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/ingestions/t1/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(response.text)
    assert [name for name, _ in frames] == ["task.t1.progress", "task.t1.done"]
    assert frames[0][1] == {"n": 1}


def test_stream_of_a_task_with_no_backlog_returns_an_empty_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StreamingAppService(status="FAILED", events=[])
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/ingestions/t1/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _sse_frames(response.text) == []


def test_stream_of_an_unknown_task_is_a_plain_enveloped_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NotFoundService(MockAppService):
        async def get_task(self, task_id: str) -> dict[str, object]:
            from harborrag_core.contracts.errors import HarborNotFoundError

            raise HarborNotFoundError("Ingestion task was not found")

    with _client(_NotFoundService(), monkeypatch) as client:
        response = client.get("/v1/ingestions/missing/stream")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "harbor_not_found_error"
