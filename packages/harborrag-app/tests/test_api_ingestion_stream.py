"""Contract tests for the ingestion task SSE stream (ML2): replay + live tail."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.ingestion.routes import stream_ingestion
from harborrag_core.contracts.events import HarborEvent


class _StreamingAppService(MockAppService):
    """MockAppService plus a test-controllable stream_ingestion_events()."""

    def __init__(self, *, status: str, events: list[HarborEvent]) -> None:
        super().__init__()
        self._status = status
        self._events = events
        self.last_after_seq: int | None = None

    async def get_task(self, task_id: str) -> dict[str, object]:
        return {"task_id": task_id, "tenant": "DEFAULT", "status": self._status}

    async def stream_ingestion_events(
        self, task_id: str, *, after_seq: int | None = None
    ) -> AsyncIterator[HarborEvent]:
        del task_id
        self.last_after_seq = after_seq
        for event in self._events:
            yield event


def _sse_frames(body: str) -> list[tuple[str, dict[str, object]]]:
    return [(name, data) for name, data, _id in _sse_frames_with_id(body)]


def _sse_frames_with_id(body: str) -> list[tuple[str, dict[str, object], str | None]]:
    if not body.strip():
        return []
    frames = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        event_id = next(
            (line.removeprefix("id: ") for line in lines if line.startswith("id: ")), None
        )
        frames.append((event, json.loads(data), event_id))
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


def test_stream_frames_carry_the_event_sequence_as_the_sse_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each frame's ``id:`` is the durable seq, so a client can resume past it."""
    events = [
        HarborEvent(name="task.t1.progress", trace_id="t1", payload={"n": 1}, seq=5),
        HarborEvent(name="task.t1.done", trace_id="t1", payload={"n": 2}, seq=6),
    ]
    service = _StreamingAppService(status="SUCCESS", events=events)
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/ingestions/t1/stream")

    frames = _sse_frames_with_id(response.text)
    assert [event_id for _, _, event_id in frames] == ["5", "6"]


def test_stream_forwards_last_event_id_header_as_the_backlog_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnecting EventSource's Last-Event-ID resumes the backlog past it."""
    service = _StreamingAppService(status="SUCCESS", events=[])
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/ingestions/t1/stream", headers={"Last-Event-ID": "5"})

    assert response.status_code == 200
    assert service.last_after_seq == 5


def test_stream_ignores_a_malformed_last_event_id_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StreamingAppService(status="SUCCESS", events=[])
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/ingestions/t1/stream", headers={"Last-Event-ID": "not-a-number"})

    assert response.status_code == 200
    assert service.last_after_seq is None


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


@pytest.mark.asyncio
async def test_stream_closes_the_app_service_generator_on_early_disconnect() -> None:
    """Closing the SSE body iterator early must close the app-service stream too.

    Starlette closes/cancels the response body iterator when a client
    disconnects mid-tail. Without ``_frames``'s ``finally: await
    events.aclose()``, that cancellation would abandon whatever the app
    service still held open (e.g. an event-bus subscription -- see
    AppService.stream_ingestion_events) to be cleaned up only whenever
    Python's GC gets around to it, instead of deterministically.
    """
    closed = {"value": False}
    progress_event = HarborEvent(name="task.t1.progress", trace_id="t1", payload={"n": 1})

    class _HangingAppService(MockAppService):
        async def get_task(self, task_id: str) -> dict[str, object]:
            return {"task_id": task_id, "tenant": "DEFAULT", "status": "RUNNING"}

        async def stream_ingestion_events(
            self, task_id: str, *, after_seq: int | None = None
        ) -> AsyncIterator[HarborEvent]:
            del task_id, after_seq
            try:
                yield progress_event
                await asyncio.Event().wait()  # never resolves -- simulates a live tail
            finally:
                closed["value"] = True

    principal = Principal(subject="test", role="reader", tenant_ids=frozenset({"*"}))
    response = await stream_ingestion(
        "t1",
        service=_HangingAppService(),
        principal=principal,
        last_event_id=None,
    )
    body = response.body_iterator
    first_chunk = await body.__anext__()
    assert b"task.t1.progress" in first_chunk
    assert closed["value"] is False

    await body.aclose()

    assert closed["value"] is True
