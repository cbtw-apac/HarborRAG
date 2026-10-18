"""Beta access on the HTTP API: the door check, the editor floor and the 403 mapping."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborAuthError, HarborConfigurationError

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
TENANT = "DEFAULT"
LISTED = "listed-user"
STRANGER = "stranger"

_CATALOG = """
version: 1
connectors:
  docs:
    provider: local
"""
_ACCESS = f"""
version: 1
tenants:
  {TENANT}:
    projects:
      beta:
        datasources: [docs]
        members:
          - principal: {LISTED}
"""
_REFUSAL = HarborAuthError("not entitled to any datasource in this tenant", forbidden=True)
_LOGGER = logging.getLogger("tests.beta_access")


def _token(
    subject: str, role: str = "reader", tenants: tuple[str, ...] = (TENANT,)
) -> dict[str, str]:
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "role": role,
        "tenants": list(tenants),
        "iat": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "iss": "harborrag",
        "aud": "harborrag-api",
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}


def _settings() -> ApiSettings:
    return ApiSettings(auth_mode="hmac", auth_secret=SECRET)


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def access_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Switch beta access on, over a one-connection catalog and one listed member."""

    catalog = tmp_path / "connectors.yaml"
    catalog.write_text(dedent(_CATALOG), encoding="utf-8")
    path = tmp_path / "access.yaml"
    path.write_text(dedent(_ACCESS), encoding="utf-8")
    monkeypatch.setenv("HARBORRAG_ACCESS_CONFIG_PATH", str(path))
    monkeypatch.setenv("HARBORRAG_CONNECTOR_CONFIG_PATH", str(catalog))
    # Shared memory scopes are refused beside an access file.
    monkeypatch.setenv("HARBORRAG_MEMORY_RECALL_SCOPES", "user,session")
    return path


@pytest.fixture
def make_client(
    monkeypatch: pytest.MonkeyPatch, service: MockAppService
) -> Callable[[], TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))

    def build() -> TestClient:
        return TestClient(create_fastapi_app(_settings()), raise_server_exceptions=False)

    return build


@pytest.fixture
def client(access_file: Path, make_client: Callable[[], TestClient]) -> TestClient:
    with make_client() as test_client:
        yield test_client


def _refuse_retrieval(service: MockAppService) -> None:
    """Make the facade refuse the way a held context with no entitled scope does."""

    async def retrieve(*_args: object, **_kwargs: object) -> AppResponse:
        return failure_response(_LOGGER, _REFUSAL)

    def graph_response(*_args: object, **_kwargs: object) -> AppResponse:
        return failure_response(_LOGGER, _REFUSAL)

    service.retrieve = retrieve  # type: ignore[method-assign]
    service._graph_response = graph_response  # type: ignore[method-assign]


_RETRIEVAL_CALLS = [
    ("/v1/retrieval/vector", {"query": "policy"}),
    ("/v1/retrieval/graph/triplets", {"subject": "a"}),
    ("/v1/retrieval/graph/paths", {"start_node": "a", "end_node": "b"}),
    ("/v1/retrieval/graph/subgraphs", {"start_node": "a"}),
    ("/v1/graph/traverse", {"start_node": "a"}),
]


@pytest.mark.parametrize(("path", "body"), _RETRIEVAL_CALLS, ids=[p for p, _ in _RETRIEVAL_CALLS])
def test_unlisted_user_gets_403_on_vector_retrieval(
    client: TestClient, service: MockAppService, path: str, body: dict[str, object]
) -> None:
    """A facade refusal reaches the caller as 403, not as an unavailable service."""

    _refuse_retrieval(service)

    response = client.post(path, json=body, headers=_token(LISTED))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "harbor_auth_error"


def test_a_failure_that_is_not_a_forbidden_refusal_stays_unavailable(
    client: TestClient, service: MockAppService
) -> None:
    async def retrieve(*_args: object, **_kwargs: object) -> AppResponse:
        return failure_response(_LOGGER, HarborAuthError("expired"))

    service.retrieve = retrieve  # type: ignore[method-assign]

    response = client.post("/v1/retrieval/vector", json={"query": "x"}, headers=_token(LISTED))

    assert response.status_code == 503


def test_task_documents_and_conflicts_need_editor_with_access_file(
    access_file: Path, make_client: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = ("/v1/ingestions/ing_1/documents", "/v1/graph/conflicts")
    with make_client() as restricted:
        for route in routes:
            reader = restricted.get(route, headers=_token(LISTED, "reader"))
            assert (route, reader.status_code) == (route, 403)
            editor = restricted.get(route, headers=_token(LISTED, "editor"))
            assert (route, editor.status_code) == (route, 200)

    monkeypatch.delenv("HARBORRAG_ACCESS_CONFIG_PATH")
    with make_client() as open_client:
        for route in routes:
            reader = open_client.get(route, headers=_token(LISTED, "reader"))
            assert (route, reader.status_code) == (route, 200)


def test_startup_refuses_non_sub_user_claim_with_access_file(access_file: Path) -> None:
    settings = ApiSettings(auth_mode="hmac", auth_secret=SECRET, auth_user_id_claim="email")

    with pytest.raises(HarborConfigurationError) as raised:
        create_fastapi_app(settings)

    message = str(raised.value)
    assert "HARBORRAG_ACCESS_CONFIG_PATH" in message
    assert "HARBORRAG_AUTH_USER_ID_CLAIM" in message


def test_startup_refuses_auth_mode_none_with_access_file(access_file: Path) -> None:
    with pytest.raises(HarborConfigurationError) as raised:
        create_fastapi_app(ApiSettings(auth_mode="none"))

    message = str(raised.value)
    assert "HARBORRAG_ACCESS_CONFIG_PATH" in message
    assert "HARBORRAG_AUTH_MODE" in message


def test_without_an_access_file_the_app_has_no_watcher_and_any_user_claim_starts() -> None:
    app = create_fastapi_app(ApiSettings(auth_user_id_claim="email"))

    assert app.state.access_watcher is None


def test_unlisted_user_is_refused_before_chat_and_agent_answer(
    client: TestClient, service: MockAppService
) -> None:
    headers = _token(STRANGER)

    chat = client.post("/v1/chat/completions", json={"prompt": "hello"}, headers=headers)
    agent = client.post("/v1/agent/completions", json={"prompt": "hello"}, headers=headers)
    connections = client.get("/v1/connections", headers=headers)

    assert [chat.status_code, agent.status_code, connections.status_code] == [403, 403, 403]
    assert chat.json()["error"]["message"] == "not listed in the access file"
    assert service.chat_calls == []
    assert service.agent_calls == []
    assert service.connection_listing_scopes == []


def test_a_missing_access_file_refuses_everyone(client: TestClient, access_file: Path) -> None:
    access_file.unlink()

    response = client.get("/v1/connections", headers=_token(LISTED))

    assert response.status_code == 403


def test_a_listed_user_reaches_the_routes(client: TestClient, service: MockAppService) -> None:
    response = client.post("/v1/chat/completions", json={"prompt": "hello"}, headers=_token(LISTED))

    assert response.status_code == 200
    assert len(service.chat_calls) == 1


def test_listed_user_keeps_listed_tenants_only(client: TestClient, service: MockAppService) -> None:
    wildcard = _token(LISTED, tenants=("*",))
    both = _token(LISTED, tenants=(TENANT, "OTHER"))
    unlisted_grant = _token(LISTED, tenants=("OTHER",))

    def search(headers: dict[str, str], tenant: str) -> int:
        return client.post(
            "/v1/retrieval/vector", json={"query": "x", "tenant": tenant}, headers=headers
        ).status_code

    assert search(wildcard, TENANT) == 200
    assert search(wildcard, "OTHER") == 403
    assert search(both, TENANT) == 200
    assert search(both, "OTHER") == 403
    # Listed for TENANT, but the token grants only OTHER: nothing is left in common.
    assert client.get("/v1/connections", headers=unlisted_grant).status_code == 403
    assert [call["tenant_id"] for call in service.retrieval_calls] == [TENANT, TENANT]
