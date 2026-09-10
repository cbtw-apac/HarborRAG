"""How a streamed chat completion reports failure, and what it promises on close.

Split from ``test_api_chat_stream.py``: that file covers the happy path and the
deadline, this one covers the ways a stream can go wrong -- what is rejected
before the status line is sent, what the terminal ``error`` frame is allowed to
say, and the cleanup the transport owes the application service.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.v1.chat import routes as chat_routes
from harborrag_app.api.v1.chat.schemas import ChatCompletionRequest
from harborrag_core.contracts.errors import HarborNoIndexedContentError


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
    response = client.post("/v1/chat/sessions", json={"tenant": tenant})
    assert response.status_code == 201
    return response.json()["session_id"]


def test_chat_stream_rejects_an_unknown_project_before_the_stream_opens(
    client: TestClient,
) -> None:
    """A project outside the tenant is a 404, not a 200 with an error frame.

    The JSON branch already answers ``404`` because the application service
    validates the scope before it commits to a turn. The stream branch has to
    ask before the headers go out, or the same request answers ``200`` with a
    body claiming the chat service is unavailable.
    """

    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={
            "prompt": "Hello",
            "session_id": session_id,
            "stream": True,
            "project_id": "missing-project",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


def test_chat_completion_rejects_an_unknown_project(client: TestClient) -> None:
    """The JSON branch the streaming branch has to agree with."""

    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "project_id": "missing-project"},
    )

    assert response.status_code == 404


def test_chat_stream_error_frame_names_a_provider_failure(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    """A provider failure is not an unavailable service; the frame must say which."""

    async def fail(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        yield {
            "kind": "error",
            "error": "ChatStreamError",
            "error_type": "ChatStreamError",
            "detail": "provider key and private endpoint",
        }

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: fail(query))
    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    frames = _sse_frames(response.text)
    assert frames == [
        ("error", {"code": "chat_stream_error", "message": "The chat provider stream failed"})
    ]
    assert "private endpoint" not in response.text


def test_chat_stream_error_frame_names_a_scope_failure(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    """A session deleted mid-flight races past the pre-check; say not-found, not 503."""

    async def gone(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        yield {
            "kind": "error",
            "error": "HarborNotFoundError",
            "error_type": "HarborNotFoundError",
        }

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: gone(query))
    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    frames = _sse_frames(response.text)
    assert frames == [
        (
            "error",
            {
                "code": "harbor_not_found_error",
                "message": "Conversation session or project was not found",
            },
        )
    ]


def test_chat_stream_error_frame_falls_back_for_an_unreviewed_failure(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    """Only reviewed error types reach the wire; anything else stays generic.

    The frame is an allowlist, not a projection of whatever class happened to
    fail, so a new internal exception cannot start naming itself to callers.
    """

    async def fail(query: str) -> AsyncIterator[dict[str, object]]:
        del query
        yield {"kind": "error", "error": "ValueError", "error_type": "PsycopgOperationalError"}

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: fail(query))
    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    frames = _sse_frames(response.text)
    assert frames == [
        ("error", {"code": "harbor_connection_error", "message": "Chat service is unavailable"})
    ]


@pytest.mark.asyncio
async def test_chat_stream_closes_the_service_generator_when_the_body_is_abandoned() -> None:
    """Abandoning the response body must close the service stream inline.

    The service persists whatever text it already delivered while still
    holding the session lock, and it does that in its own ``GeneratorExit``
    handling. None of it runs unless something awaits the generator's close:
    an ``async for`` that is merely abandoned leaves the work to asyncgen
    finalization at some later, unordered moment -- after the lock has gone.
    """

    closed: list[str] = []

    async def chat_stream(query: str, **_: object) -> AsyncIterator[dict[str, object]]:
        del query
        try:
            yield {"kind": "chunk", "chunk": {"event": "text_delta", "content": "Hel"}}
            yield {"kind": "chunk", "chunk": {"event": "completed", "finish_reason": "stop"}}
        finally:
            closed.append("service stream")

    response = chat_routes._stream_response(
        ChatCompletionRequest(session_id="session-1", prompt="Hello"),
        SimpleNamespace(chat_stream=chat_stream),  # type: ignore[arg-type]
        Principal(subject="reader-1", role="reader", tenant_ids=frozenset({"DEFAULT"})),
        timeout_seconds=5.0,
    )

    body = response.body_iterator
    assert b"text_delta" in await anext(body)  # type: ignore[arg-type]
    await body.aclose()  # type: ignore[attr-defined]

    assert closed == ["service stream"]


def test_chat_completions_documents_both_response_media_types(client: TestClient) -> None:
    """``stream: true`` returns SSE, so the schema has to say so.

    One route serves JSON and ``text/event-stream``. Advertising only the
    completion model tells a generated client the streaming shape does not
    exist.
    """

    schema = client.app.openapi()  # type: ignore[attr-defined]
    content = schema["paths"]["/v1/chat/completions"]["post"]["responses"]["200"]["content"]

    assert set(content) == {"application/json", "text/event-stream"}
    # Declaring the extra media type must not displace the generated one: the
    # JSON half still has to name the completion model.
    assert content["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ChatCompletionResponse"
    }


def test_chat_stream_and_json_responses_are_both_uncacheable(client: TestClient) -> None:
    session_id = _session(client)
    body = {"prompt": "Hello", "session_id": session_id}

    json_response = client.post("/v1/chat/completions", json=body)
    stream_response = client.post("/v1/chat/completions", json={**body, "stream": True})

    assert json_response.headers["cache-control"] == "no-store"
    assert stream_response.headers["cache-control"] == "no-store"


def test_chat_completion_reports_an_empty_index_as_409(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    """Nothing ingested is the caller's to fix, so it must not read as a 503."""

    async def no_index(query: str, **_: object) -> object:
        del query
        raise HarborNoIndexedContentError(
            "No content has been ingested yet, so there is nothing to search"
        )

    monkeypatch.setattr(service, "chat_completion", no_index)
    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "harbor_no_indexed_content_error"


def test_chat_stream_names_an_empty_index_in_its_error_frame(
    client: TestClient,
    service: MockAppService,
    monkeypatch,
) -> None:
    """The streaming counterpart of the 409, since status is already sent."""

    async def no_index(query: str, **_: object) -> AsyncIterator[dict[str, object]]:
        del query
        yield {
            "kind": "error",
            "error": "HarborNoIndexedContentError",
            "error_type": "HarborNoIndexedContentError",
        }

    monkeypatch.setattr(service, "chat_stream", lambda query, **_: no_index(query))
    session_id = _session(client)
    response = client.post(
        "/v1/chat/completions",
        json={"prompt": "Hello", "session_id": session_id, "stream": True},
    )

    frames = _sse_frames(response.text)
    assert frames == [
        (
            "error",
            {
                "code": "no_indexed_content",
                "message": "No content has been ingested yet, so there is nothing to search",
            },
        )
    ]
