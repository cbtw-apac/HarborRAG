"""An API key reads documents as its owner over HTTP chat, as it does over MCP (D3)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.domain.project import Project
from harborrag_engine.security.api_key_service import VerifiedKey
from harborrag_runtime.application.control_plane.connection_models import ConnectionKey
from harborrag_runtime.contracts import RetrievalResponse
from harborrag_runtime.sdk import RetrievalLane
from harborrag_runtime.testing.chat_fakes import FakeRetrievalFacade
from harborrag_runtime.testing.chat_service_double import PASSAGES
from harborrag_runtime.testing.connection_fakes import (
    JIRA_WRITE,
    ConnectionWorld,
    connection_world,
)

pytestmark = pytest.mark.unit

_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"
_KEY = "hrk_dev_v1_0123456789abcdef01234567." + "k" * 43
_KEY_ID = "0123456789abcdef01234567"
_OWNER = "svc-demo"
_TENANT = {"X-Tenant-Id": "acme"}


class _Keys:
    """API key verifier double that knows one key, owned by ``_OWNER``."""

    async def verify_key(self, raw_key: str) -> VerifiedKey | None:
        if raw_key != _KEY:
            return None
        return VerifiedKey(
            key_id=_KEY_ID,
            tenant_id="acme",
            owner=_OWNER,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )


class _SourceAclRetrieval(FakeRetrievalFacade):
    """Returns the passages the request's principal may read.

    ``doc_1`` is granted to ``_OWNER`` only. ``doc_2`` is public but explicitly denied
    to ``_OWNER``.
    """

    async def search(self, request: object) -> RetrievalResponse:
        await super().search(request)
        principal = request.access.principal_id  # type: ignore[attr-defined]
        readable = {"doc_1"} if principal == _OWNER else {"doc_2"}
        return RetrievalResponse(
            request_id="retrieval-1",
            lane=RetrievalLane.HYBRID,
            results=tuple(
                result for result in self.results if result.metadata["document_id"] in readable
            ),
            diagnostics={},
        )


def _jwt(**extra: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": "u_alex",
        "role": "reader",
        "tenants": ["acme"],
        "iat": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "iss": "harborrag",
        "aud": "harborrag-api",
        **extra,
    }
    return jwt.encode(claims, _SECRET, algorithm="HS256")


@pytest.fixture
def service() -> MockAppService:
    double = MockAppService()
    double.chat_runtime.retrieval = _SourceAclRetrieval(PASSAGES)
    return double


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: MockAppService) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=_SECRET, auth_user_id_claim="email")
    app = create_fastapi_app(settings)
    with TestClient(app) as test_client:
        app.state.api_key_verifier = _Keys()
        yield test_client


def _cited_documents(response_body: dict[str, object]) -> set[str]:
    citations = response_body["citations"]
    assert isinstance(citations, list)
    return {citation["document_id"] for citation in citations}


def test_api_key_chat_reads_the_owners_grant_and_honours_the_owners_deny(
    client: TestClient, service: MockAppService
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"message": "Which runbook covers a deploy?"},
        headers={"Authorization": f"Bearer {_KEY}", **_TENANT},
    )

    assert response.status_code == 200, response.text
    assert service.retrieval.request.access.principal_id == _OWNER  # type: ignore[union-attr]
    assert _cited_documents(response.json()) == {"doc_1"}


def test_api_key_session_still_belongs_to_the_key(
    client: TestClient, service: MockAppService
) -> None:
    headers = {"Authorization": f"Bearer {_KEY}", **_TENANT}
    created = client.post("/v1/chat/sessions", json={}, headers=headers)
    session_id = created.json()["session_id"]

    response = client.post(
        "/v1/chat/completions",
        json={"session_id": session_id, "message": "Which runbook covers a deploy?"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert ("acme", _KEY_ID, _OWNER, session_id) in service.conversation_sessions
    assert service.retrieval.request.access.principal_id == _OWNER  # type: ignore[union-attr]


async def _two_connections(world: ConnectionWorld) -> None:
    for connection_id in ("demo-tickets", "demo-wiki"):
        await world.service.put(
            ConnectionKey("acme", "demo", connection_id),
            replace(JIRA_WRITE, name=f"DEMO {connection_id}"),
            actor="u_alex",
        )


def test_api_key_lists_the_connections_its_owner_may_search(
    client: TestClient, service: MockAppService
) -> None:
    world = connection_world([Project(id="demo", name="DEMO", collection="demo", tenant_id="acme")])
    asyncio.run(_two_connections(world))
    world.runs.scopes = {"demo/demo-tickets": ["scope-tickets"], "demo/demo-wiki": ["scope-wiki"]}
    world.runs.readable = {_OWNER: ("scope-tickets",), _KEY_ID: ("scope-wiki",)}
    service.connection_service = world.service  # type: ignore[attr-defined]

    by_key = client.get(
        "/v1/projects/demo/connections", headers={"Authorization": f"Bearer {_KEY}", **_TENANT}
    )
    by_user = client.get(
        "/v1/projects/demo/connections",
        headers={"Authorization": f"Bearer {_jwt(email='alex@example.com')}", **_TENANT},
    )

    assert by_key.status_code == 200, by_key.text
    assert [item["connection_ref"] for item in by_key.json()["items"]] == ["demo/demo-tickets"]
    assert by_user.json()["items"] == []


def test_user_token_chat_still_reads_as_its_subject(
    client: TestClient, service: MockAppService
) -> None:
    token = _jwt(email="alex@example.com")

    response = client.post(
        "/v1/chat/completions",
        json={"message": "Which runbook covers a deploy?"},
        headers={"Authorization": f"Bearer {token}", **_TENANT},
    )

    assert response.status_code == 200, response.text
    assert service.retrieval.request.access.principal_id == "u_alex"  # type: ignore[union-attr]
