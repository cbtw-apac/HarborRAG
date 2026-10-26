"""A caller may choose a model, bounded by what its tenant is allowed.

The rule itself lives in the runtime, which is the only layer that can see
both a tenant's own catalog and the process-wide one. What these tests pin is
the transport contract: the chosen name reaches the turn, a name outside the
tenant's bound is a ``422`` naming the field, and omitting it changes nothing.
"""

from __future__ import annotations

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


def _chat_session(client: TestClient, tenant: str = "DEFAULT") -> str:
    response = client.post("/v1/chat/sessions", json={"tenant": tenant})
    assert response.status_code == 201
    return str(response.json()["session_id"])


def _agent_session(client: TestClient, tenant: str = "DEFAULT") -> str:
    response = client.post("/v1/agent/sessions", json={"tenant": tenant})
    assert response.status_code == 201
    return str(response.json()["session_id"])


def _completion(client: TestClient, session_id: str, **extra: object) -> object:
    return client.post(
        "/v1/chat/completions",
        json={"tenant": "DEFAULT", "session_id": session_id, "prompt": "Hi", **extra},
    )


def test_an_allowed_model_reaches_the_turn(client: TestClient, service: MockAppService) -> None:
    service.tenant_models["DEFAULT"] = {"tenant-fast"}
    session_id = _chat_session(client)

    response = _completion(client, session_id, model="tenant-fast")

    assert response.status_code == 200  # type: ignore[attr-defined]
    assert service.chat_calls[-1]["model"] == "tenant-fast"


def test_a_model_the_tenant_catalog_does_not_configure_is_a_422(
    client: TestClient, service: MockAppService
) -> None:
    service.tenant_models["DEFAULT"] = {"tenant-fast"}
    session_id = _chat_session(client)

    response = _completion(client, session_id, model="primary")

    assert response.status_code == 422  # type: ignore[attr-defined]
    error = response.json()["error"]  # type: ignore[attr-defined]
    assert error["code"] == "harbor_validation_error"
    assert error["details"] == {"field": "model"}
    # The turn never started, so nothing was spent and nothing remembered.
    assert service.chat_calls == []


def test_an_unknown_model_with_no_tenant_catalog_is_a_422(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _chat_session(client)

    response = _completion(client, session_id, model="gpt-imaginary")

    assert response.status_code == 422  # type: ignore[attr-defined]
    assert service.chat_calls == []


def test_omitting_the_model_leaves_the_turn_unchanged(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _chat_session(client)

    response = _completion(client, session_id)

    assert response.status_code == 200  # type: ignore[attr-defined]
    assert service.chat_calls[-1]["model"] is None


def test_a_stream_rejects_a_disallowed_model_before_any_frame(
    client: TestClient, service: MockAppService
) -> None:
    # Validation happens before the stream/JSON branch, so a rejected name is
    # a status code rather than a terminal error frame mid-body.
    session_id = _chat_session(client)

    response = _completion(client, session_id, model="gpt-imaginary", stream=True)

    assert response.status_code == 422  # type: ignore[attr-defined]
    assert service.chat_calls == []


def test_a_blank_model_is_rejected_by_the_schema(
    client: TestClient, service: MockAppService
) -> None:
    session_id = _chat_session(client)

    assert _completion(client, session_id, model="").status_code == 422  # type: ignore[attr-defined]


def test_the_agent_surface_bounds_the_model_the_same_way(
    client: TestClient, service: MockAppService
) -> None:
    service.tenant_models["DEFAULT"] = {"tenant-fast"}
    session_id = _agent_session(client)

    allowed = client.post(
        "/v1/agent/completions",
        json={
            "tenant": "DEFAULT",
            "session_id": session_id,
            "prompt": "Hi",
            "model": "tenant-fast",
        },
    )
    assert allowed.status_code == 200
    assert service.agent_calls[-1]["model"] == "tenant-fast"

    rejected = client.post(
        "/v1/agent/completions",
        json={
            "tenant": "DEFAULT",
            "session_id": session_id,
            "prompt": "Hi",
            "model": "primary",
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["details"] == {"field": "model"}


def test_a_resumed_agent_run_takes_no_model_of_its_own(
    client: TestClient, service: MockAppService
) -> None:
    # A checkpoint is never continued under a different model, so the field is
    # deliberately absent from the resume contract.
    session_id = _agent_session(client)
    started = client.post(
        "/v1/agent/completions",
        json={"tenant": "DEFAULT", "session_id": session_id, "prompt": "Hi"},
    )
    assert started.status_code == 200

    resumed = client.post(
        f"/v1/agent/runs/{started.json()['run_id']}/resume",
        json={"tenant": "DEFAULT", "session_id": session_id, "model": "primary"},
    )

    assert resumed.status_code == 422
