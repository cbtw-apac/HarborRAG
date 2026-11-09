"""A streamed completion that cannot be answered fails as JSON before the stream opens."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.models.errors import HarborChatConnectionError

PATH = "/v1/chat/completions"


@pytest.fixture
def service() -> MockAppService:
    double = MockAppService()
    double.known_projects = {"cpm", "fresh"}
    return double


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


@pytest.mark.parametrize("stream", [False, True])
def test_an_unknown_project_is_refused_before_any_frame(
    client: TestClient, service: MockAppService, stream: bool
) -> None:
    response = client.post(
        PATH, json={"message": "Hello", "project_ids": ["missing"], "stream": stream}
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "reference_not_found"
    assert not service.chat.requests


@pytest.mark.parametrize("stream", [False, True])
def test_an_empty_project_is_a_409_before_any_frame(
    client: TestClient, service: MockAppService, stream: bool
) -> None:
    response = client.post(
        PATH, json={"message": "Hello", "project_ids": ["fresh"], "stream": stream}
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "no_indexed_content"


@pytest.mark.parametrize("stream", [False, True])
def test_an_unreachable_model_is_a_503_before_any_frame(
    client: TestClient, service: MockAppService, stream: bool
) -> None:
    service.chat.failure = HarborChatConnectionError("provider unreachable")

    response = client.post(PATH, json={"message": "Hello", "stream": stream})

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "llm_unavailable"


def test_chat_completions_documents_both_response_media_types(client: TestClient) -> None:
    schema = client.app.openapi()  # type: ignore[attr-defined]
    content = schema["paths"][PATH]["post"]["responses"]["200"]["content"]

    assert set(content) == {"application/json", "text/event-stream"}
    assert content["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ChatCompletionResponse"
    }


def test_chat_stream_and_json_responses_are_both_uncacheable(client: TestClient) -> None:
    json_response = client.post(PATH, json={"message": "Hello"})
    stream_response = client.post(PATH, json={"message": "Hello", "stream": True})

    assert json_response.headers["cache-control"] == "no-store"
    assert stream_response.headers["cache-control"] == "no-store"
