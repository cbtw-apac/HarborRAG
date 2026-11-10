"""Contract tests for the ingestion task SSE stream (ML2): replay + live tail."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import pytest
from app_test_fixtures import MockAppService
from fastapi import Response
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.dependencies import ResponseContext
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.ingestion.routes import stream_ingestion
from harborrag_app.operations import LocalOperations, Scope
from harborrag_core.contracts.events import HarborEvent


class _StreamingAppService(MockAppService):
    """MockAppService plus a test-controllable stream_ingestion_events()."""

    def __init__(self, *, events: list[HarborEvent]) -> None:
        super().__init__()
        self._events = events
        self.last_after_seq: int | None = None

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


def _context(deadline_seconds: float = 600.0) -> ResponseContext:
    settings = ApiSettings.model_construct(api_stream_timeout_seconds=deadline_seconds)
    return ResponseContext(settings, Response())


def _client(service: MockAppService, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    return TestClient(create_fastapi_app(ApiSettings()))


def test_stream_replays_backlog_and_closes_for_an_already_terminal_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal task's stream is pure backlog replay -- no live tail is opened."""
    events = [
        HarborEvent(name="task.t1.run.status", trace_id="t1", payload={"n": 1}),
        HarborEvent(name="task.t1.run.completed", trace_id="t1", payload={"n": 2}),
    ]
    service = _StreamingAppService(events=events)
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/admin/ingestions/t1/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(response.text)
    assert [name for name, _ in frames] == ["run.status", "run.completed"]
    assert frames[0][1] == {"n": 1}


def test_stream_frames_carry_the_event_sequence_as_the_sse_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each frame's ``id:`` is the durable seq, so a client can resume past it."""
    events = [
        HarborEvent(name="task.t1.run.status", trace_id="t1", payload={"n": 1}, seq=5),
        HarborEvent(name="task.t1.run.completed", trace_id="t1", payload={"n": 2}, seq=6),
    ]
    service = _StreamingAppService(events=events)
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/admin/ingestions/t1/events")

    frames = _sse_frames_with_id(response.text)
    assert [event_id for _, _, event_id in frames] == ["5", "6"]


def test_stream_forwards_last_event_id_header_as_the_backlog_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnecting EventSource's Last-Event-ID resumes the backlog past it."""
    service = _StreamingAppService(events=[])
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/admin/ingestions/t1/events", headers={"Last-Event-ID": "5"})

    assert response.status_code == 200
    assert service.last_after_seq == 5


def test_stream_ignores_a_malformed_last_event_id_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StreamingAppService(events=[])
    with _client(service, monkeypatch) as client:
        response = client.get(
            "/v1/admin/ingestions/t1/events", headers={"Last-Event-ID": "not-a-number"}
        )

    assert response.status_code == 200
    assert service.last_after_seq is None


def test_stream_of_a_task_with_no_backlog_returns_an_empty_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StreamingAppService(events=[])
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/admin/ingestions/t1/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _sse_frames(response.text) == []


def test_stream_of_another_tenants_run_is_a_plain_enveloped_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(MockAppService(), monkeypatch) as client:
        response = client.get("/v1/admin/ingestions/t1/events", headers={"X-Tenant-Id": "globex"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_found"


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
    progress_event = HarborEvent(name="task.t1.run.status", trace_id="t1", payload={"n": 1}, seq=1)

    class _HangingAppService(MockAppService):
        async def stream_ingestion_events(
            self, task_id: str, *, after_seq: int | None = None
        ) -> AsyncIterator[HarborEvent]:
            del task_id, after_seq
            try:
                yield progress_event
                await asyncio.Event().wait()  # never resolves -- simulates a live tail
            finally:
                closed["value"] = True

    response = await stream_ingestion(
        "t1",
        operations=LocalOperations(_HangingAppService()),
        scope=Scope(
            Principal(subject="test", role="editor", tenant_ids=frozenset({"*"})), "DEFAULT"
        ),
        context=_context(),
        last_event_id=None,
    )
    body = response.body_iterator
    first_chunk = await body.__anext__()
    assert first_chunk.startswith(b"id: 1\nevent: run.status\n")
    assert closed["value"] is False

    await body.aclose()

    assert closed["value"] is True


@pytest.mark.asyncio
async def test_stream_sends_a_ping_comment_while_the_run_is_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("harborrag_app.api.v1.ingestion.routes.HEARTBEAT_SECONDS", 0.01)

    class _QuietAppService(MockAppService):
        async def stream_ingestion_events(
            self, task_id: str, *, after_seq: int | None = None
        ) -> AsyncIterator[HarborEvent]:
            del task_id, after_seq
            yield HarborEvent(name="task.t1.run.status", trace_id="t1", seq=1)
            await asyncio.sleep(0.05)
            yield HarborEvent(name="task.t1.run.completed", trace_id="t1", seq=2)

    response = await stream_ingestion(
        "t1",
        operations=LocalOperations(_QuietAppService()),
        scope=Scope(
            Principal(subject="test", role="editor", tenant_ids=frozenset({"*"})), "DEFAULT"
        ),
        context=_context(),
        last_event_id=None,
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert b": ping\n\n" in chunks
    assert chunks[-1].startswith(b"id: 2\nevent: run.completed\n")


@pytest.mark.asyncio
async def test_disconnect_while_idle_between_pings_closes_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("harborrag_app.api.v1.ingestion.routes.HEARTBEAT_SECONDS", 0.01)
    closed = {"value": False}

    class _IdleAppService(MockAppService):
        async def stream_ingestion_events(
            self, task_id: str, *, after_seq: int | None = None
        ) -> AsyncIterator[HarborEvent]:
            del task_id, after_seq
            try:
                yield HarborEvent(name="task.t1.run.status", trace_id="t1", seq=1)
                await asyncio.Event().wait()
            finally:
                closed["value"] = True

    response = await stream_ingestion(
        "t1",
        operations=LocalOperations(_IdleAppService()),
        scope=Scope(
            Principal(subject="test", role="editor", tenant_ids=frozenset({"*"})), "DEFAULT"
        ),
        context=_context(),
        last_event_id=None,
    )
    body = response.body_iterator
    await body.__anext__()
    assert await body.__anext__() == b": ping\n\n"
    consumer = asyncio.ensure_future(body.__anext__())
    await asyncio.sleep(0.005)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await body.aclose()

    assert closed["value"] is True


@pytest.mark.asyncio
async def test_stream_past_its_deadline_ends_with_a_deadline_exceeded_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("harborrag_app.api.v1.ingestion.routes.HEARTBEAT_SECONDS", 1.0)

    class _EndlessAppService(MockAppService):
        async def stream_ingestion_events(
            self, task_id: str, *, after_seq: int | None = None
        ) -> AsyncIterator[HarborEvent]:
            del task_id, after_seq
            yield HarborEvent(name="task.t1.run.status", trace_id="t1", seq=1)
            await asyncio.Event().wait()

    response = await stream_ingestion(
        "t1",
        operations=LocalOperations(_EndlessAppService()),
        scope=Scope(
            Principal(subject="test", role="editor", tenant_ids=frozenset({"*"})), "DEFAULT"
        ),
        context=_context(0.05),
        last_event_id=None,
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks[0].startswith(b"id: 1\nevent: run.status\n")
    assert chunks[-1].startswith(b"event: error\n")
    assert json.loads(chunks[-1].split(b"data: ")[1])["code"] == "deadline_exceeded"


def test_route_streams_the_whole_run_sequence_with_increasing_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = [
        (
            "run.status",
            {
                "status": "running",
                "counters": {"discovered": 2, "processed": 0, "skipped": 0, "failed": 0},
            },
        ),
        ("stage.progress", {"stage": "publish", "done": 1, "total": 2}),
        (
            "document.failed",
            {"document_id": "d1", "stage": "interpret", "error_category": "parse_failed"},
        ),
        (
            "run.status",
            {
                "status": "paused",
                "counters": {"discovered": 2, "processed": 1, "skipped": 0, "failed": 1},
            },
        ),
        (
            "run.completed",
            {
                "counters": {"discovered": 2, "processed": 1, "skipped": 0, "failed": 1},
                "finish_reason": "failed",
            },
        ),
    ]
    events = [
        HarborEvent(name=f"task.t1.{name}", trace_id="t1", payload=payload, seq=index * 10)
        for index, (name, payload) in enumerate(sequence, start=1)
    ]
    with _client(_StreamingAppService(events=events), monkeypatch) as client:
        response = client.get("/v1/admin/ingestions/t1/events")

    frames = _sse_frames_with_id(response.text)
    assert [name for name, _, _ in frames] == [name for name, _ in sequence]
    assert [payload for _, payload, _ in frames] == [payload for _, payload in sequence]
    ids = [int(event_id) for _, _, event_id in frames]
    assert ids == sorted(set(ids))


class _Operations:
    def __init__(self, status: str) -> None:
        self._status = status

    async def get(self, *, tenant_id: str, ingestion_id: str) -> dict[str, object]:
        return {"ingestion_id": ingestion_id, "tenant_id": tenant_id, "status": self._status}


class _QuietAppService(MockAppService):
    """A run with nothing to send: the stream stays open until the caller leaves."""

    def __init__(self, status: str = "paused") -> None:
        super().__init__()
        self.ingestion_operations = _Operations(status)

    async def stream_ingestion_events(
        self, task_id: str, *, after_seq: int | None = None
    ) -> AsyncIterator[HarborEvent]:
        del task_id, after_seq
        await asyncio.Event().wait()
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_a_quiet_run_opens_at_once_and_ends_with_a_deadline_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("harborrag_app.api.v1.ingestion.routes.HEARTBEAT_SECONDS", 0.01)
    response = await stream_ingestion(
        "t1",
        operations=LocalOperations(_QuietAppService()),
        scope=Scope(
            Principal(subject="test", role="editor", tenant_ids=frozenset({"*"})), "DEFAULT"
        ),
        context=_context(0.1),
        last_event_id="7",
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert b": ping\n\n" in chunks
    assert chunks[-1].startswith(b"event: error\n")
    assert json.loads(chunks[-1].split(b"data: ")[1])["code"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_a_quiet_run_pings_and_outlives_the_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream deadline is longer than the request deadline: the stream must not be cut."""
    monkeypatch.setattr("harborrag_app.api.v1.ingestion.routes.HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (_QuietAppService(), "test"))
    settings = ApiSettings(api_request_timeout_seconds=1.0, api_stream_timeout_seconds=5.0)
    app = create_fastapi_app(settings)
    sent: list[dict[str, object]] = []
    closed = asyncio.Event()

    requested = False

    async def receive() -> dict[str, object]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await closed.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/admin/ingestions/t1/events",
        "raw_path": b"/v1/admin/ingestions/t1/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"last-event-id", b"7"), (b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "state": {},
    }
    async with app.router.lifespan_context(app):
        call = asyncio.ensure_future(app(scope, receive, send))
        await asyncio.sleep(1.6)
        still_open = not call.done()
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call

    assert still_open
    start = next(m for m in sent if m["type"] == "http.response.start")
    pings = [m for m in sent if m.get("body") == b": ping\n\n"]
    assert start["status"] == 200
    assert len(pings) >= 5


def test_reconnecting_to_an_ended_run_with_nothing_after_the_id_answers_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StreamingAppService(events=[])
    service.ingestion_operations = _Operations("succeeded")
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/admin/ingestions/t1/events", headers={"Last-Event-ID": "9"})

    assert response.status_code == 204
    assert response.content == b""


def test_an_ended_run_with_events_after_the_id_still_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [HarborEvent(name="task.t1.run.completed", trace_id="t1", payload={}, seq=9)]
    service = _StreamingAppService(events=events)
    service.ingestion_operations = _Operations("succeeded")
    with _client(service, monkeypatch) as client:
        response = client.get("/v1/admin/ingestions/t1/events", headers={"Last-Event-ID": "8"})

    assert response.status_code == 200
    assert [name for name, _ in _sse_frames(response.text)] == ["run.completed"]
