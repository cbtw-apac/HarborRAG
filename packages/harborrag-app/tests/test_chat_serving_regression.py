"""Serving-path regressions for /v1/chat/sessions and /v1/chat/completions.

Every test here enters through the real ``AppService`` composition -- real
``ConversationSessionService``, real SQLite-backed
``SqlConversationMemoryRepository`` -- because the existing contract tests
substitute the whole application service at ``app.state.app_service`` and
therefore cannot observe any of this wiring.

Generation is faked at the ``AsyncHarborChatClientProtocol`` boundary, i.e.
exactly what ``ChatClientFactory`` produces for ``HarborChatClient``. A
regression to a direct ``litellm``/``openai`` call inside the engine bypasses
this double and fails the assertion that it was invoked.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_core.contracts.errors import HarborConfigurationError, HarborConnectionError
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
)
from harborrag_runtime.chat import RuntimeChatService
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import RetrievalLane
from harborrag_runtime.retrieval import RetrievalDiagnostics, RuntimeRetrievalReport
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

ANSWER = "HarborRAG indexes your documents and answers from them."
DOCUMENT_ID = "doc-alpha"
CHUNK_ID = "chunk-alpha-1"


class FakeChatClient:
    """Stand in for the client ``ChatClientFactory`` builds from the catalogue."""

    def __init__(self) -> None:
        self.requests: list[HarborChatRequest] = []
        self.closed = False

    async def achat(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        del messages, model, kwargs
        assert request is not None
        self.requests.append(request)
        return HarborChatResponse(
            id="resp-1",
            created=1,
            logical_model="primary",
            provider="openai",
            provider_model="openai/test-chat-model",
            deployment="openai-primary",
            message=HarborChatMessage.assistant(ANSWER),
            finish_reason="stop",
        )

    def astream(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        raise NotImplementedError("streaming is not exercised by these tests")

    async def aclose(self) -> None:
        self.closed = True


class FakeRetrievalService:
    """Return one citable result, or fail like an unreachable vector store."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls = 0

    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: str,
        top_k: int,
        access: Any,
        options: Any,
    ) -> RuntimeRetrievalReport:
        del query, tenant_id, top_k, access, options
        self.calls += 1
        if self.unavailable:
            raise HarborConnectionError("vector store is unreachable")
        return RuntimeRetrievalReport(
            request_id="req-1",
            lane=RetrievalLane.HYBRID,
            results=(
                RetrievalResult(
                    id=CHUNK_ID,
                    text="HarborRAG is a retrieval-augmented generation platform.",
                    score=0.87,
                    metadata={"document_id": DOCUMENT_ID},
                ),
            ),
            diagnostics=RetrievalDiagnostics(
                candidate_hits=1,
                stale_candidates=0,
                unpublished_candidates=0,
                malformed_candidates=0,
                search_window=1,
                graph_nodes=0,
                graph_relations=0,
                graph_truncated=False,
                duration_ms=1.0,
            ),
        )

    async def aclose(self) -> None:
        return None


def _runtime(settings: RuntimeSettings, chat_client: FakeChatClient, retrieval: Any) -> HarborRAG:
    """Real SDK runtime with only the two outbound edges replaced."""

    runtime = HarborRAG(HarborRAGConfig(runtime=settings))
    runtime._chat_runtime = RuntimeChatService(settings, client_builder=lambda _s: chat_client)
    runtime._retrieval = retrieval
    return runtime


def _app_service(
    settings: RuntimeSettings,
    chat_client: FakeChatClient,
    retrieval: Any,
) -> AppService:
    composition = CompositionRoot.production(settings)
    return AppService(
        composition,
        settings,
        factories=AppServiceFactories(
            retrieval_runtime=lambda _s: _runtime(settings, chat_client, retrieval)
        ),
    )


@pytest.fixture
def chat_client() -> FakeChatClient:
    return FakeChatClient()


@pytest.fixture
def retrieval() -> FakeRetrievalService:
    return FakeRetrievalService()


@pytest.fixture
def settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/serving.db")


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    settings: RuntimeSettings,
    chat_client: FakeChatClient,
    retrieval: FakeRetrievalService,
) -> TestClient:
    service = _app_service(settings, chat_client, retrieval)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "production"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def _create_session(client: TestClient) -> dict[str, Any]:
    response = client.post("/v1/chat/sessions", json={"tenant": "DEFAULT"})
    assert response.status_code == 201, response.text
    return response.json()


def _complete(client: TestClient, session_id: str, prompt: str = "What is HarborRAG?"):  # noqa: ANN202
    return client.post(
        "/v1/chat/completions",
        json={"tenant": "DEFAULT", "session_id": session_id, "prompt": prompt},
    )


POOL = frozenset(
    {
        "Hello! How can I help you today?",
        "Hi! What would you like to explore?",
        "Welcome! Ask me anything about your indexed knowledge.",
    }
)


def test_session_returns_one_random_greeting_as_an_assistant_message(
    client: TestClient,
) -> None:
    """The greeting ships in the envelope a completion uses for its answer."""

    payloads = [_create_session(client) for _ in range(20)]

    for payload in payloads:
        assert payload["session_id"].startswith("session-")
        assert set(payload) == {"session_id", "message"}
        message = payload["message"]
        assert message["role"] == "assistant"
        assert message["content"] in POOL
    assert len({payload["session_id"] for payload in payloads}) == 20
    # Twenty draws from a three-entry pool land on one value with probability
    # 3 * (1/3)**20; a single value means the draw is not random at all.
    assert len({payload["message"]["content"] for payload in payloads}) > 1


def test_greetings_come_from_configuration_not_a_module_constant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    chat_client: FakeChatClient,
    retrieval: FakeRetrievalService,
) -> None:
    """An environment override must change what the endpoint serves."""

    monkeypatch.setenv("HARBORRAG_CHAT_GREETINGS", json.dumps(["Ahoy!", "Welcome aboard!"]))
    overridden = RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/override.db")
    service = _app_service(overridden, chat_client, retrieval)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "production"))

    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        drawn = {_create_session(test_client)["message"]["content"] for _ in range(20)}

    assert drawn <= {"Ahoy!", "Welcome aboard!"}
    assert drawn.isdisjoint(POOL)


@pytest.mark.parametrize(
    "greetings",
    [(), ("  ",)],
    ids=["empty-pool", "blank-entry"],
)
def test_an_undrawable_greeting_pool_fails_at_startup(greetings: tuple[str, ...]) -> None:
    """``secrets.choice`` must never be reached with nothing to draw."""

    with pytest.raises(ValueError, match="HARBORRAG_CHAT_GREETINGS"):
        RuntimeSettings(chat_greetings=greetings)


def test_incomplete_model_configuration_fails_the_boot_not_the_request(
    monkeypatch: pytest.MonkeyPatch,
    settings: RuntimeSettings,
    chat_client: FakeChatClient,
    retrieval: FakeRetrievalService,
) -> None:
    """Regression: an unset ``${HARBOR_*}`` surfaced as a per-request 503."""

    service = _app_service(settings, chat_client, retrieval)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "production"))
    monkeypatch.delenv("HARBOR_CHAT_PROVIDER")

    with pytest.raises(HarborConfigurationError, match="model configuration is incomplete"):
        with TestClient(create_fastapi_app(ApiSettings())):
            pass


def test_completion_answers_with_citations_and_persists_the_turn(
    client: TestClient,
    chat_client: FakeChatClient,
    retrieval: FakeRetrievalService,
) -> None:
    session_id = _create_session(client)["session_id"]

    response = _complete(client, session_id)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["message"]["content"] == ANSWER
    assert payload["message"]["role"] == "assistant"
    assert payload["session_id"] == session_id
    assert payload["citations"] == [
        {"document_id": DOCUMENT_ID, "chunk_id": CHUNK_ID, "score": 0.87}
    ]
    assert retrieval.calls == 1
    # Generation went through the chat-client port, not a direct provider call.
    assert len(chat_client.requests) == 1
    assert chat_client.requests[0].logical_model is None, "the call site must not pin a model"

    second = _complete(client, session_id, "And what does it cite?")
    assert second.status_code == 200, second.text
    # The persisted first turn is replayed: system + user/assistant pair + new user.
    replayed = chat_client.requests[1].messages
    assert [message.role.value for message in replayed] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert replayed[2].content == ANSWER


def test_unknown_session_is_404_not_a_500(client: TestClient) -> None:
    response = _complete(client, "session-does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


def test_empty_prompt_is_422(client: TestClient) -> None:
    session_id = _create_session(client)["session_id"]

    response = _complete(client, session_id, "")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_retrieval_outage_is_503_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    chat_client: FakeChatClient,
) -> None:
    settings = RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/outage.db")
    service = _app_service(settings, chat_client, FakeRetrievalService(unavailable=True))
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "production"))

    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        session_id = _create_session(test_client)["session_id"]
        response = _complete(test_client, session_id)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "harbor_connection_error"
    assert "Traceback" not in response.text
    assert body["error"]["details"] == {}


def test_fifty_sessions_and_completions_do_not_exhaust_the_pool(
    client: TestClient,
    chat_client: FakeChatClient,
) -> None:
    for _ in range(50):
        session_id = _create_session(client)["session_id"]
        assert _complete(client, session_id).status_code == 200

    assert len(chat_client.requests) == 50


def test_agent_session_contract_is_unchanged(client: TestClient) -> None:
    """The agent surface is out of scope: it must still publish one greeting.

    ``ConversationSessionService`` is shared with the agent routes. The
    chat-side move to an assistant-message envelope happens in
    ``ChatClientMixin`` alone, so the agent payload must be untouched --
    nothing else covers this, since the agent contract tests substitute the
    whole application service.
    """

    response = client.post("/v1/agent/sessions", json={"tenant": "DEFAULT"})

    assert response.status_code == 201, response.text
    payload = response.json()
    assert isinstance(payload["greeting"], str)
    assert payload["greeting"]
    assert "message" not in payload


class DeadConversationMemory:
    """A control database that has gone away after startup."""

    _MESSAGE = "control database is unreachable"

    async def create(self, identity: Any) -> None:
        raise OSError(self._MESSAGE)

    async def exists(self, identity: Any) -> bool:
        raise OSError(self._MESSAGE)

    async def recent(self, identity: Any, *, limit: int = 2) -> tuple[Any, ...]:
        raise OSError(self._MESSAGE)

    async def append(self, identity: Any, turn: Any) -> None:
        raise OSError(self._MESSAGE)

    async def clear(self, identity: Any) -> None:
        raise OSError(self._MESSAGE)


@pytest.fixture
def dead_memory_client(
    monkeypatch: pytest.MonkeyPatch,
    settings: RuntimeSettings,
    chat_client: FakeChatClient,
    retrieval: FakeRetrievalService,
) -> TestClient:
    service = _app_service(settings, chat_client, retrieval)
    dead = DeadConversationMemory()
    service._sessions._repository = dead
    service._chat._memory = dead
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "production"))
    with TestClient(
        create_fastapi_app(ApiSettings()), raise_server_exceptions=False
    ) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/chat/sessions", {"tenant": "DEFAULT"}),
        (
            "/v1/chat/completions",
            {"tenant": "DEFAULT", "session_id": "session-abc", "prompt": "hi"},
        ),
        (
            "/v1/chat/completions",
            {
                "tenant": "DEFAULT",
                "session_id": "session-abc",
                "prompt": "hi",
                "stream": True,
            },
        ),
    ],
    ids=["session", "completion", "streaming-precheck"],
)
def test_conversation_store_outage_is_503_not_500(
    dead_memory_client: TestClient,
    path: str,
    body: dict[str, Any],
) -> None:
    """Regression: a control-database outage escaped as an unhandled 500.

    ``False`` from the session store means "unknown session" and answers 404,
    so an outage must not be flattened into that answer either.
    """

    response = dead_memory_client.post(path, json=body)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "harbor_connection_error"
    assert "Traceback" not in response.text
    assert "unreachable" not in response.text
