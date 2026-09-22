"""Contract tests for the provider screen API (CRUD, test-connection, routing, cost)."""

from __future__ import annotations

import pytest
from app_test_control_plane import control_plane_app_service
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.dependencies import get_principal
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.settings import ApiSettings


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def test_list_providers_never_reveals_a_raw_secret(client: TestClient) -> None:
    response = client.get("/v1/providers")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == "prov_1"
    assert payload[0]["secret_ref"] == "secret://db/1"


def test_get_provider_returns_the_seeded_provider(client: TestClient) -> None:
    response = client.get("/v1/providers/prov_1")

    assert response.status_code == 200
    assert response.json()["name"] == "OpenAI Production"


def test_get_unknown_provider_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/providers/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


def test_create_provider_never_returns_the_raw_api_key(
    client: TestClient, service: MockAppService
) -> None:
    response = client.post(
        "/v1/providers",
        json={
            "tenant_id": "ACME",
            "name": "Anthropic Prod",
            "family": "chat",
            "config": {"model": "anthropic/claude-3-5-haiku-20241022"},
            "api_key": "sk-super-secret-value",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert "sk-super-secret-value" not in response.text
    assert payload["secret_ref"] is not None
    call = service.provider_create_calls[-1]
    assert call["tenant_id"] == "ACME"
    assert call["api_key"] == "sk-super-secret-value"
    assert call["actor"] == "dev"


def test_update_provider_rotates_the_secret_reference(
    client: TestClient, service: MockAppService
) -> None:
    response = client.patch("/v1/providers/prov_1", json={"api_key": "sk-new-value"})

    assert response.status_code == 200
    payload = response.json()
    assert "sk-new-value" not in response.text
    assert payload["secret_ref"] == "secret://db/rotated"
    call = service.provider_update_calls[-1]
    assert call["provider_id"] == "prov_1"
    assert call["updates"] == {"api_key": "sk-new-value"}


def test_update_provider_rejects_explicit_null_config(client: TestClient) -> None:
    response = client.patch("/v1/providers/prov_1", json={"config": None})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_delete_provider_removes_it(client: TestClient, service: MockAppService) -> None:
    response = client.delete("/v1/providers/prov_1")

    assert response.status_code == 204
    assert "prov_1" not in service.providers
    assert service.provider_delete_calls[-1]["provider_id"] == "prov_1"


def test_test_provider_connection_reports_success_for_chat_family(client: TestClient) -> None:
    response = client.post("/v1/providers/prov_1/test")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["latency_ms"] >= 0


def test_test_provider_connection_rejects_a_non_chat_family(
    client: TestClient, service: MockAppService
) -> None:
    from harborrag_core.domain.provider import Provider

    service.providers["prov_embed"] = Provider(
        id="prov_embed",
        tenant_id="DEFAULT",
        name="Embeddings",
        family="embedding",
        config={"model": "openai/text-embedding-3-small"},
    )

    response = client.post("/v1/providers/prov_embed/test")

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "harbor_capability_error"


def test_test_provider_connection_unknown_provider_is_not_found(client: TestClient) -> None:
    response = client.post("/v1/providers/does-not-exist/test")

    assert response.status_code == 404


def test_get_routing_rules_returns_the_seeded_rule(client: TestClient) -> None:
    response = client.get("/v1/providers/routing")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["provider_id"] == "prov_1"


def test_replace_routing_rules_updates_the_table(
    client: TestClient, service: MockAppService
) -> None:
    response = client.put(
        "/v1/providers/routing",
        json=[{"family": "chat", "provider_id": "prov_1", "priority": 1}],
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["priority"] == 1
    assert service.routing_replace_calls[-1]["actor"] == "dev"


def test_replace_routing_rules_rejects_a_tenant_scoped_admin() -> None:
    """A workspace-wide table cannot be replaced by an admin of one tenant."""
    app = create_fastapi_app(ApiSettings())
    service = control_plane_app_service()
    app.dependency_overrides[get_app_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="scoped-admin", role="admin", tenant_ids=frozenset({"tenant-a"})
    )

    with TestClient(app) as client:
        response = client.put(
            "/v1/providers/routing",
            json=[{"family": "chat", "provider_id": "prov_1", "priority": 1}],
        )

    assert response.status_code == 403


def test_replace_routing_rules_rejects_an_unknown_provider(client: TestClient) -> None:
    response = client.put(
        "/v1/providers/routing",
        json=[{"family": "chat", "provider_id": "does-not-exist"}],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


def test_get_provider_cost_reports_zero_for_a_provider_with_no_usage(client: TestClient) -> None:
    response = client.get("/v1/providers/cost")

    assert response.status_code == 200
    payload = response.json()
    assert payload["providers"] == {"prov_1": 0.0}
    assert "since" in payload


def test_create_provider_rejects_a_tenant_outside_the_callers_claim() -> None:
    """Creating a provider for a tenant the caller cannot access must 403, not silently create it."""
    app = create_fastapi_app(ApiSettings())
    service = control_plane_app_service()
    app.dependency_overrides[get_app_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="scoped-user", role="admin", tenant_ids=frozenset({"tenant-a"})
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/providers",
            json={
                "tenant_id": "tenant-b",
                "name": "Blocked",
                "family": "chat",
                "config": {},
            },
        )

    assert response.status_code == 403


def test_update_provider_outside_the_callers_tenants_404s() -> None:
    """Updating a provider outside the caller's tenant scope must 404, not silently apply."""
    app = create_fastapi_app(ApiSettings())
    service = control_plane_app_service()
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        created = client.post(
            "/v1/providers",
            json={"tenant_id": "tenant-a", "name": "Docs", "family": "chat", "config": {}},
        ).json()

        app.dependency_overrides[get_principal] = lambda: Principal(
            subject="other-tenant-user", role="admin", tenant_ids=frozenset({"tenant-b"})
        )
        response = client.patch(f"/v1/providers/{created['id']}", json={"name": "Hacked"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "harbor_not_found_error"


def test_provider_outside_the_callers_tenants_404s_not_403s() -> None:
    """Existence of a provider in another tenant must not leak via a 403."""
    app = create_fastapi_app(ApiSettings())
    service = control_plane_app_service()
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        created = client.post(
            "/v1/providers",
            json={"tenant_id": "tenant-a", "name": "Docs", "family": "chat", "config": {}},
        ).json()

        # Sanity: the default (owner, unrestricted) principal really can see it.
        assert client.get(f"/v1/providers/{created['id']}").status_code == 200
        assert len(client.get("/v1/providers").json()) == 1

        app.dependency_overrides[get_principal] = lambda: Principal(
            subject="other-tenant-user", role="owner", tenant_ids=frozenset({"tenant-b"})
        )
        assert client.get(f"/v1/providers/{created['id']}").status_code == 404
        assert client.get("/v1/providers").json() == []
