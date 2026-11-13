"""Story 5 AC5-AC12: ``POST /v1/chat/completions`` as JSON on the explorer contract.

Every request runs the production chat pipeline over a fake model and fake retrieval
(``MockAppService``), so these tests see what the pipeline sends to the model and to
retrieval as well as what it answers and stores.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from app_test_chat import ANSWER
from app_test_conversations import ALICE, auth, hmac_app
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_core.models.errors import HarborChatConnectionError
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.blackbox

COMPLETIONS = "/v1/chat/completions"
COMPLETION_FIELDS = {
    "completion_id",
    "session_id",
    "trace_id",
    "mode",
    "answer",
    "citations",
    "usage",
}
CITATION_FIELDS = {"n", "chunk_id", "document_id", "title", "snippet", "url", "page"}
USAGE_FIELDS = {
    "mode",
    "reasoning",
    "steps",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cost_usd",
}
QUESTION = "Who has Go and Kubernetes experience in Hanoi?"
KEY = "5f0c2b1e-8d4a-4c3e-9f7b-1a2b3c4d5e6f"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
MARKER = re.compile(r"\[(\d+)\]")


@pytest.fixture
def service() -> MockAppService:
    double = MockAppService()
    double.tenant_projects = {"DEFAULT": {"cpm", "empty", "other"}}
    double.catalog.add("cpm", "cpm/cpm-tickets", ("scope-cpm-tickets",), documents=4)
    double.catalog.add("cpm", "cpm/cpm-wiki", ("scope-cpm-wiki",), documents=3)
    double.catalog.add("other", "other/conn", ("scope-other",), documents=2)
    double.catalog.add("empty", "empty/new-board", (), documents=0)
    return double


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    with TestClient(hmac_app(monkeypatch, service)) as test_client:
        yield test_client


def _session(client: TestClient, project_id: str | None = "cpm") -> str:
    body = {} if project_id is None else {"project_id": project_id}
    response = client.post("/v1/chat/sessions", json=body, headers=auth())
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def _ask(
    client: TestClient, body: dict[str, object], **headers: str
) -> tuple[int, dict[str, object]]:
    response = client.post(COMPLETIONS, json=body, headers={**auth(), **headers})
    return response.status_code, response.json()


def _history(client: TestClient, session_id: str) -> list[dict[str, object]]:
    response = client.get(f"/v1/chat/sessions/{session_id}/messages", headers=auth())
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def _field(body: dict[str, object]) -> str:
    error = body["error"]
    assert isinstance(error, dict)
    details = error["details"]
    if isinstance(details, list):
        return str(details[0]["field"])
    assert isinstance(details, dict)
    return str(details["field"])


def _code(body: dict[str, object]) -> str:
    error = body["error"]
    assert isinstance(error, dict)
    return str(error["code"])


def test_a_classic_answer_has_exactly_the_explorer_fields(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)

    status, body = _ask(
        client,
        {"session_id": session_id, "message": QUESTION, "mode": "classic", "stream": False},
        **{"X-Trace-Id": TRACE},
    )

    assert status == 200, body
    assert set(body) == COMPLETION_FIELDS
    assert body["session_id"] == session_id
    assert body["mode"] == "classic"
    assert body["trace_id"] == TRACE
    assert str(body["completion_id"]).startswith("msg_")
    citations = body["citations"]
    assert isinstance(citations, list) and citations
    assert all(set(citation) == CITATION_FIELDS for citation in citations)
    usage = body["usage"]
    assert isinstance(usage, dict)
    assert set(usage) == USAGE_FIELDS
    assert usage["mode"] == "classic"
    assert usage["reasoning"] == "off"
    assert usage["steps"] == 1
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (4, 2)
    assert usage["reasoning_tokens"] == 0
    assert usage["cost_usd"] is None


def test_usage_adds_up_every_model_call_of_the_turn(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)
    service.chat.cost = 0.001

    _, classic = _ask(client, {"session_id": session_id, "message": QUESTION})
    _, instant = _ask(client, {"session_id": session_id, "message": QUESTION, "mode": "instant"})

    assert classic["usage"]["prompt_tokens"] == 4  # type: ignore[index]
    assert classic["usage"]["cost_usd"] == pytest.approx(0.002)  # type: ignore[index]
    assert instant["usage"]["prompt_tokens"] == 2  # type: ignore[index]
    assert instant["usage"]["cost_usd"] == pytest.approx(0.001)  # type: ignore[index]


def test_every_answer_marker_has_a_citation_and_every_citation_a_marker(
    client: TestClient,
) -> None:
    session_id = _session(client)

    _, body = _ask(client, {"session_id": session_id, "message": QUESTION})

    answer = str(body["answer"])
    citations = body["citations"]
    assert isinstance(citations, list)
    markers = {int(number) for number in MARKER.findall(answer)}
    assert markers == {citation["n"] for citation in citations} == {1, 2}
    assert (
        answer
        == "Two candidates match: CPM-4411 [1] and CPM-4502 [2]; see also. CPM-4411 [1] led it."
    )
    first, second = citations
    assert (first["n"], first["chunk_id"], first["document_id"]) == (1, "chk_4411", "doc_4411")
    assert first["title"] == "CPM-4411 Senior Go engineer, Hanoi"
    assert first["page"] == 2 and first["url"] is None
    assert len(first["snippet"]) <= 300
    assert (second["n"], second["chunk_id"]) == (2, "chk_4502")
    assert second["url"] == "https://jira.example.test/browse/CPM-4502" and second["page"] is None
    assert second["snippet"] == "Seven years of Go services and Kubernetes operators in Hanoi."


def test_retrieval_reads_only_the_session_projects_connections(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)

    _ask(client, {"session_id": session_id, "message": QUESTION})

    request = service.retrieval.request
    assert request is not None
    assert request.filters == {"source_scope_id": ["scope-cpm-tickets", "scope-cpm-wiki"]}


def test_the_model_calls_carry_the_completions_trace_id(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)

    _, body = _ask(client, {"session_id": session_id, "message": QUESTION}, **{"X-Trace-Id": TRACE})

    assert body["trace_id"] == TRACE
    assert service.chat.requests[0].metadata.trace_id == TRACE
    assert service.chat.gate_requests[0].metadata.trace_id == TRACE


def test_instant_skips_the_scope_check_call_and_graph_observation(
    client: TestClient, service: MockAppService
) -> None:
    service.use_chat_settings(RuntimeSettings(chat_retrieval_graph_search=True))
    session_id = _session(client)

    classic_status, classic = _ask(client, {"session_id": session_id, "message": QUESTION})
    classic_retrieval = service.retrieval.request
    instant_status, instant = _ask(
        client, {"session_id": session_id, "message": QUESTION, "mode": "instant"}
    )
    instant_retrieval = service.retrieval.request

    assert (classic_status, instant_status) == (200, 200)
    assert classic["mode"] == "classic" and instant["mode"] == "instant"
    assert instant["usage"]["mode"] == "instant"  # type: ignore[index]
    assert len(service.chat.gate_requests) == 1
    assert len(service.chat.requests) == 2
    assert classic_retrieval.observe_graph is True  # type: ignore[union-attr]
    assert instant_retrieval.observe_graph is False  # type: ignore[union-attr]
    assert instant_retrieval.top_k == classic_retrieval.top_k  # type: ignore[union-attr]
    assert instant_retrieval.filters == classic_retrieval.filters  # type: ignore[union-attr]


def test_a_one_off_question_answers_without_creating_a_session(
    client: TestClient, service: MockAppService
) -> None:
    status, body = _ask(client, {"message": QUESTION, "project_ids": ["cpm"]})

    assert status == 200, body
    assert body["session_id"] is None
    assert set(body) == COMPLETION_FIELDS
    assert client.get("/v1/chat/sessions", headers=auth()).json()["items"] == []
    request = service.retrieval.request
    assert request is not None
    assert request.filters == {"source_scope_id": ["scope-cpm-tickets", "scope-cpm-wiki"]}


def test_a_one_off_question_in_an_unknown_project_is_a_reference_not_found(
    client: TestClient, service: MockAppService
) -> None:
    status, body = _ask(client, {"message": QUESTION, "project_ids": ["nope"]})

    assert status == 422
    assert _code(body) == "reference_not_found"
    assert _field(body) == "project_ids"
    assert not service.chat.requests and not service.chat.gate_requests


def test_project_ids_inside_a_session_are_refused(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)

    status, body = _ask(
        client, {"session_id": session_id, "message": QUESTION, "project_ids": ["other"]}
    )

    assert status == 422
    assert _code(body) == "validation_error"
    assert _field(body) == "project_ids"
    assert not service.chat.requests and not service.chat.gate_requests


def test_a_connection_outside_the_sessions_project_is_out_of_scope(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)

    status, body = _ask(
        client, {"session_id": session_id, "message": QUESTION, "connections": ["other/conn"]}
    )

    assert status == 422
    assert _code(body) == "connection_out_of_scope"
    assert not service.chat.requests and not service.chat.gate_requests


def test_a_connection_inside_the_scope_narrows_retrieval_to_it(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)

    status, _ = _ask(
        client,
        {"session_id": session_id, "message": QUESTION, "connections": ["cpm/cpm-wiki"]},
    )

    assert status == 200
    assert service.retrieval.request.filters == {"source_scope_id": ["scope-cpm-wiki"]}  # type: ignore[union-attr]


def test_a_filter_narrows_retrieval_and_an_unsupported_one_is_refused(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)
    supported = {
        "and": [
            {"field": "connection", "op": "in", "value": ["cpm/cpm-tickets"]},
            {"field": "document_kind", "op": "eq", "value": "jira_issue"},
        ]
    }

    ok_status, _ = _ask(
        client, {"session_id": session_id, "message": QUESTION, "filters": supported}
    )
    or_status, or_body = _ask(
        client,
        {
            "session_id": session_id,
            "message": QUESTION,
            "filters": {"or": [{"field": "document_kind", "op": "eq", "value": "page"}]},
        },
    )

    assert ok_status == 200
    assert service.retrieval.request.filters == {  # type: ignore[union-attr]
        "document_kind": "jira_issue",
        "source_scope_id": ["scope-cpm-tickets"],
    }
    assert or_status == 422
    assert _field(or_body) == "filters"


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "source_scope_id", "op": "eq", "value": 5},
        {"field": "source_scope_id", "op": "in", "value": [5]},
        {"field": "document_kind", "op": "gte", "value": 3},
        {"field": "document_kind", "op": "contains", "value": "jira"},
        {"field": "no_such_key", "op": "eq", "value": "x"},
    ],
    ids=["numeric-scope", "numeric-scope-list", "range-on-keyword", "contains", "unindexed"],
)
def test_a_filter_retrieval_cannot_apply_is_refused_on_filters(
    client: TestClient, service: MockAppService, condition: dict[str, object]
) -> None:
    session_id = _session(client)

    status, body = _ask(
        client, {"session_id": session_id, "message": QUESTION, "filters": {"and": [condition]}}
    )

    assert status == 422, body
    assert _code(body) == "validation_error"
    assert _field(body) == "filters"
    assert service.retrieval.request is None


@pytest.mark.parametrize(
    ("body", "code", "field"),
    [
        ({"message": ""}, "validation_error", "message"),
        ({"message": "x" * 8001}, "validation_error", "message"),
        ({"message": "  \n\t "}, "validation_error", "message"),
        ({"message": QUESTION, "mode": "advanced"}, "mode_unavailable", "mode"),
        ({"message": QUESTION, "mode": "auto"}, "mode_unavailable", "mode"),
        ({"message": QUESTION, "mode": "bogus"}, "validation_error", "mode"),
        ({"message": QUESTION, "reasoning": "high"}, "validation_error", "reasoning"),
        ({"message": QUESTION, "reasoning": "low"}, "validation_error", "reasoning"),
    ],
    ids=[
        "empty",
        "too-long",
        "blank",
        "advanced",
        "auto",
        "bogus-mode",
        "reasoning-high",
        "reasoning-low",
    ],
)
def test_invalid_completion_input_is_refused_before_any_model_call(
    client: TestClient,
    service: MockAppService,
    body: dict[str, object],
    code: str,
    field: str,
) -> None:
    session_id = _session(client)

    status, error = _ask(client, {"session_id": session_id, **body})

    assert status == 422
    assert _code(error) == code
    assert _field(error) == field
    assert not service.chat.requests and not service.chat.gate_requests


def test_eight_thousand_characters_are_accepted(client: TestClient) -> None:
    session_id = _session(client)

    status, _ = _ask(client, {"session_id": session_id, "message": "x" * 8000})

    assert status == 200


@pytest.mark.parametrize(
    "retired",
    [
        {"prompt": QUESTION},
        {"idempotency_key": KEY},
        {"tenant_id": "DEFAULT"},
        {"model": "primary"},
        {"graph_search": True},
        {"project_id": "cpm"},
    ],
    ids=["prompt", "idempotency_key", "tenant_id", "model", "graph_search", "project_id"],
)
def test_retired_request_fields_are_refused_without_an_alias(
    client: TestClient, retired: dict[str, object]
) -> None:
    session_id = _session(client)

    status, body = _ask(client, {"session_id": session_id, "message": QUESTION, **retired})

    assert status == 422
    assert _field(body) == next(iter(retired))


def test_an_empty_project_is_no_indexed_content_before_any_model_call(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client, "empty")

    status, body = _ask(client, {"session_id": session_id, "message": QUESTION})

    assert status == 409
    assert _code(body) == "no_indexed_content"
    assert not service.chat.requests and not service.chat.gate_requests
    assert service.retrieval.request is None


def test_an_unreachable_model_is_llm_unavailable_and_stores_no_answer(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)
    service.chat.failure = HarborChatConnectionError("provider refused the connection")

    status, body = _ask(client, {"session_id": session_id, "message": QUESTION})

    assert status == 503
    assert _code(body) == "llm_unavailable"
    assert [item for item in _history(client, session_id) if item["role"] == "assistant"] == []


def test_an_unknown_session_is_not_found(client: TestClient, service: MockAppService) -> None:
    status, body = _ask(client, {"session_id": "ses_nope", "message": QUESTION})

    assert status == 404
    assert _code(body) == "not_found"
    assert not service.chat.requests and not service.chat.gate_requests


def test_the_session_projects_top_k_applies_live(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)
    service.settings_overrides.overrides = {"project:cpm": 2, "tenant": 7}

    _ask(client, {"session_id": session_id, "message": QUESTION})
    first = service.retrieval.request.top_k  # type: ignore[union-attr]
    service.settings_overrides.overrides = {"project:cpm": 3, "tenant": 7}
    _ask(client, {"session_id": session_id, "message": QUESTION})
    second = service.retrieval.request.top_k  # type: ignore[union-attr]

    assert (first, second) == (2, 3)


def test_a_one_off_question_reads_its_one_projects_top_k(
    client: TestClient, service: MockAppService
) -> None:
    service.settings_overrides.overrides = {"project:cpm": 2, "tenant": 7}

    _ask(client, {"message": QUESTION, "project_ids": ["cpm"]})

    assert service.retrieval.request.top_k == 2  # type: ignore[union-attr]


def test_the_completion_equals_the_newest_history_message(client: TestClient) -> None:
    session_id = _session(client)

    _, body = _ask(client, {"session_id": session_id, "message": QUESTION})
    history = _history(client, session_id)

    newest, question = history[0], history[1]
    assert newest["message_id"] == body["completion_id"]
    assert newest["content"] == body["answer"]
    assert newest["citations"] == body["citations"]
    assert newest["trace_id"] == body["trace_id"]
    assert newest["mode"] == body["mode"] == "classic"
    assert (question["role"], question["content"]) == ("user", QUESTION)


def test_an_idempotent_retry_replays_the_same_bytes_without_a_model_call(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)
    body = {"session_id": session_id, "message": QUESTION, "mode": "classic", "stream": False}

    first = client.post(
        COMPLETIONS, json=body, headers={**auth(), "Idempotency-Key": KEY, "X-Trace-Id": TRACE}
    )
    second = client.post(
        COMPLETIONS,
        json=body,
        headers={**auth(), "Idempotency-Key": KEY, "X-Trace-Id": "0" * 31 + "1"},
    )

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.content == second.content
    assert (first.headers["Idempotency-Replayed"], second.headers["Idempotency-Replayed"]) == (
        "false",
        "true",
    )
    assert len(service.chat.requests) == 1
    assert len(service.chat.gate_requests) == 1
    history = _history(client, session_id)
    assert [item["role"] for item in history] == ["assistant", "user"]
    assert history[0]["message_id"] == second.json()["completion_id"]
    assert history[0]["trace_id"] == second.json()["trace_id"] == TRACE


@pytest.mark.parametrize(
    "change",
    [{"message": "Who knows Rust?"}, {"mode": "instant"}, {"session_id": None}],
    ids=["message", "mode", "session"],
)
def test_the_same_key_with_a_different_body_is_refused(
    client: TestClient, service: MockAppService, change: dict[str, object]
) -> None:
    session_id = _session(client)
    other_session = _session(client)
    body = {"session_id": session_id, "message": QUESTION}
    first = client.post(COMPLETIONS, json=body, headers={**auth(), "Idempotency-Key": KEY})
    changed = {**body, **change}
    if change.get("session_id", "") is None:
        changed["session_id"] = other_session

    status, error = _ask(client, changed, **{"Idempotency-Key": KEY})

    assert first.status_code == 200
    assert status == 422
    assert _code(error) == "validation_error"
    assert _field(error) == "Idempotency-Key"
    assert len(service.chat.requests) == 1


def test_an_out_of_scope_question_is_an_ordinary_answer_without_citations(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)
    service.chat.scope = "unsupported"

    status, body = _ask(client, {"session_id": session_id, "message": "Write a snake game"})

    assert status == 200, body
    assert set(body) == COMPLETION_FIELDS
    assert body["citations"] == []
    assert body["answer"]
    assert not service.chat.requests
    assert (body["usage"]["prompt_tokens"], body["usage"]["completion_tokens"]) == (2, 1)  # type: ignore[index]
    assert _history(client, session_id)[0]["message_id"] == body["completion_id"]


def test_the_fixture_answer_cites_out_of_order_and_beyond_retrieval() -> None:
    assert MARKER.findall(ANSWER) == ["2", "1", "9", "2"]


def test_a_reader_of_another_tenant_cannot_use_its_session(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _session(client)

    response = client.post(
        COMPLETIONS,
        json={"session_id": session_id, "message": QUESTION},
        headers=auth(ALICE, tenants=("GLOBEX",)),
    )

    assert response.status_code == 404
    assert not service.chat.requests
