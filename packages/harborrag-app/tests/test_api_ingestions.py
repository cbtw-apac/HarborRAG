"""Contract tests for the connector-independent public ingestion API."""

from __future__ import annotations

from uuid import UUID

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborNotFoundError


@pytest.fixture
def service() -> MockAppService:
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service: MockAppService) -> TestClient:
    monkeypatch.setattr(
        api_app,
        "select_app_service",
        lambda: (service, "test"),
    )
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "connection_id",
    [
        "confluence-main",
        "jira-main",
        "harborrag-workspace",
    ],
)
def test_valid_connector_requests_return_accepted(
    client: TestClient,
    service: MockAppService,
    connection_id: str,
) -> None:
    response = client.post(
        "/v1/ingestions",
        json={"connection_id": connection_id},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    assert UUID(response.json()["task_id"])
    assert "workflow" not in response.json()
    assert service.submissions[-1].connection_id == connection_id
    assert service.submissions[-1].tenant_id == "DEFAULT"


def test_connection_id_is_required(client: TestClient) -> None:
    response = client.post("/v1/ingestions", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


def test_unknown_request_fields_and_credentials_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/ingestions",
        json={
            "connection_id": "jira-main",
            "api_token": "must-not-enter-the-control-plane",
        },
    )

    assert response.status_code == 422
    assert "must-not-enter-the-control-plane" not in response.text


def test_connector_configuration_cannot_be_overridden_in_the_api(client: TestClient) -> None:
    response = client.post(
        "/v1/ingestions",
        json={
            "connection_id": "harborrag-workspace",
            "paths": ["another-root"],
            "include_attachments": False,
        },
    )

    assert response.status_code == 422


def test_repeated_idempotency_key_returns_the_same_task(client: TestClient) -> None:
    payload = {"connection_id": "harborrag-workspace"}
    first = client.post(
        "/v1/ingestions",
        json=payload,
        headers={"Idempotency-Key": "local-smoke-001"},
    )
    second = client.post(
        "/v1/ingestions",
        json=payload,
        headers={"Idempotency-Key": "local-smoke-001"},
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]


def test_tenant_and_force_mode_are_preserved_in_the_application_command(
    client: TestClient,
    service: MockAppService,
) -> None:
    response = client.post(
        "/v1/ingestions",
        json={
            "connection_id": "harborrag-workspace",
            "tenant": "ACME",
            "mode": "force",
        },
    )

    assert response.status_code == 202
    command = service.submissions[-1]
    assert command.tenant_id == "ACME"
    assert command.force_reprocess is True


def test_task_status_is_connector_independent_and_temporal_free(client: TestClient) -> None:
    response = client.get("/v1/ingestions/ing_1")

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "PROCESSING_DOCUMENTS"
    assert body["progress"]["processed"] == 1
    forbidden = {"workflow_id", "activity_id", "task_queue", "retry_attempt"}
    assert not (forbidden & set(body))
    assert not any(name in response.text for name in forbidden)


def test_metrics_labels_task_requests_by_route_template(client: TestClient) -> None:
    client.get("/v1/ingestions/ing_1")

    metrics = client.get("/api/v1/metrics").text
    assert 'route="/v1/ingestions/{task_id}"' in metrics
    assert 'route="/v1/ingestions/ing_1"' not in metrics


def test_document_results_support_status_and_cursor_parameters(client: TestClient) -> None:
    response = client.get(
        "/v1/ingestions/ing_1/documents",
        params={"status": "SUCCESS", "cursor": "opaque", "limit": 25},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["source_item_id"] == "adr/0001.md"
    assert item["active_document_version_id"] == "document-version:1"


def test_cancellation_is_asynchronous_and_terminal_tasks_conflict(client: TestClient) -> None:
    accepted = client.post("/v1/ingestions/ing_1/cancel")
    conflict = client.post("/v1/ingestions/complete/cancel")

    assert accepted.status_code == 202
    assert accepted.json()["message"] == "Cancellation requested"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "INGESTION_ALREADY_COMPLETED"


def test_pause_and_resume_are_asynchronous_and_terminal_tasks_conflict(
    client: TestClient,
) -> None:
    pause_accepted = client.post("/v1/ingestions/ing_1/pause")
    pause_conflict = client.post("/v1/ingestions/complete/pause")

    assert pause_accepted.status_code == 202
    assert pause_accepted.json()["message"] == "Pause requested"
    assert pause_conflict.status_code == 409
    assert pause_conflict.json()["error"]["code"] == "INGESTION_ALREADY_COMPLETED"

    resume_accepted = client.post("/v1/ingestions/ing_1/resume")
    resume_conflict = client.post("/v1/ingestions/complete/resume")

    assert resume_accepted.status_code == 202
    assert resume_accepted.json()["message"] == "Resume requested"
    assert resume_conflict.status_code == 409
    assert resume_conflict.json()["error"]["code"] == "INGESTION_ALREADY_COMPLETED"


def test_retry_failures_accepts_selected_or_all_documents(client: TestClient) -> None:
    selected = client.post(
        "/v1/ingestions/ing_1/retry-failures",
        json={"document_ids": ["document:1"]},
    )
    all_failures = client.post("/v1/ingestions/ing_1/retry-failures")

    assert selected.status_code == all_failures.status_code == 202
    assert selected.json()["accepted_document_count"] == 1
    assert UUID(selected.json()["retry_task_id"])


def test_retry_failures_rejects_an_oversized_document_id_list(client: TestClient) -> None:
    response = client.post(
        "/v1/ingestions/ing_1/retry-failures",
        json={"document_ids": [f"document:{i}" for i in range(1001)]},
    )

    assert response.status_code == 422


def test_retry_failures_rejects_an_oversized_document_id(client: TestClient) -> None:
    response = client.post(
        "/v1/ingestions/ing_1/retry-failures",
        json={"document_ids": ["x" * 256]},
    )

    assert response.status_code == 422


def test_legacy_temporal_routes_are_deprecated_compatibility_adapters(
    client: TestClient,
) -> None:
    started = client.post(
        "/api/v1/ingestions",
        json={"tenant_id": "DEFAULT", "connector_name": "harborrag-workspace"},
    )
    status = client.get("/api/v1/ingestions/ing_1")
    result = client.get("/api/v1/ingestions/ing_1/result")
    controlled = client.post(
        "/api/v1/ingestions/ing_1/actions",
        json={"action": "pause"},
    )

    assert {started.status_code, status.status_code, result.status_code} <= {200, 202}
    assert controlled.json()["action"] == "pause"
    for response in (started, status, result, controlled):
        assert response.headers["deprecation"] == "true"

    assert client.get("/v1/ingestions/ing_1/result").status_code == 404
    assert client.post("/v1/ingestions/ing_1/actions", json={}).status_code == 404


def test_legacy_retry_action_delegates_to_current_retry_service(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ingestions/ing_1/actions",
        json={"action": "retry", "artifact_ids": ["document:1"]},
    )

    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    assert response.json()["accepted_document_count"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "retry"},
        {"action": "pause", "artifact_ids": ["document:1"]},
        {"action": "retry", "artifact_ids": [" "]},
        {"action": "retry", "artifact_ids": ["document:1", "document:1"]},
    ],
)
def test_legacy_actions_retain_request_validation(
    client: TestClient,
    payload: dict[str, object],
) -> None:
    response = client.post("/api/v1/ingestions/ing_1/actions", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "harbor_validation_error"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            AppResponse(False, {"error_type": "WorkflowNotFoundError"}, "missing"),
            (404, "ingestion_run_not_found"),
        ),
        (AppResponse(False), (502, "ingestion_operation_failed")),
    ],
)
def test_legacy_status_maps_runtime_failures(
    client: TestClient,
    service: MockAppService,
    monkeypatch: pytest.MonkeyPatch,
    failure: AppResponse,
    expected: tuple[int, str],
) -> None:
    async def fail(_run_id: str) -> AppResponse:
        return failure

    monkeypatch.setattr(service, "ingestion_status", fail)
    response = client.get("/api/v1/ingestions/ing_1")

    expected_status, expected_code = expected
    assert response.status_code == expected_status
    assert response.headers["deprecation"] == "true"
    assert response.json()["error"]["code"] == expected_code


def test_legacy_start_maps_an_unavailable_connection_to_a_uniform_404(
    client: TestClient,
    service: MockAppService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy path must not reveal whether a refused connection exists."""

    async def refuse(**_kwargs: object) -> AppResponse:
        # ConnectorNotFoundError is a ValueError, so public_error_message
        # forwards its full (uniform) message.
        return AppResponse(
            False,
            {"error_type": "ConnectorNotFoundError"},
            "Unknown configured connector: 'another-tenants-connection'",
        )

    monkeypatch.setattr(service, "start_ingestion", refuse)
    response = client.post(
        "/api/v1/ingestions",
        json={"tenant_id": "DEFAULT", "connector_name": "another-tenants-connection"},
    )

    assert response.status_code == 404
    assert response.headers["deprecation"] == "true"
    error = response.json()["error"]
    assert error["code"] == "connection_not_found"
    assert error["message"] == "Connection was not found"


@pytest.mark.parametrize(
    "case",
    [
        ("/v1/ingestions/ing_1/retry-failures", None, "harbor_not_found_error"),
        (
            "/api/v1/ingestions/ing_1/actions",
            {"action": "retry", "artifact_ids": ["document:1"]},
            "connection_not_found",
        ),
    ],
)
def test_retry_with_withdrawn_connection_access_is_a_404(
    client: TestClient,
    service: MockAppService,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, dict[str, str] | None, str],
) -> None:
    url, body, expected_code = case

    async def refuse(**_kwargs: object) -> dict[str, object]:
        raise HarborNotFoundError("Connection was not found")

    monkeypatch.setattr(service, "retry_failures", refuse)
    response = client.post(url, json=body)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == expected_code
    assert response.json()["error"]["message"] == "Connection was not found"


def test_lifespan_runs_the_unbound_connection_check_once(
    monkeypatch: pytest.MonkeyPatch, service: MockAppService
) -> None:
    calls: list[int] = []
    service.warn_unbound_connections = lambda: calls.append(1)  # type: ignore[attr-defined]
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))

    with TestClient(create_fastapi_app(ApiSettings())):
        pass

    assert calls == [1]


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("ConnectorDisabledError", (404, "connection_not_found", "Connection was not found")),
        (
            "ConnectorConfigurationError",
            (500, "connector_configuration_unavailable", "Connector configuration is unavailable"),
        ),
    ],
)
def test_legacy_start_hides_connector_configuration_text(
    client: TestClient,
    service: MockAppService,
    monkeypatch: pytest.MonkeyPatch,
    error_type: str,
    expected: tuple[int, str, str],
) -> None:
    status, code, message = expected

    async def refuse(**_kwargs: object) -> AppResponse:
        # Connector errors are ValueErrors, so public_error_message forwards
        # their full text, which can name secrets and catalog paths.
        return AppResponse(
            False,
            {"error_type": error_type},
            "Connector 'x' failed: /srv/harborrag/sentinel-detail.yaml",
        )

    monkeypatch.setattr(service, "start_ingestion", refuse)
    response = client.post(
        "/api/v1/ingestions",
        json={"tenant_id": "DEFAULT", "connector_name": "x"},
    )

    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"] == message
    assert "sentinel-detail" not in response.text


@pytest.mark.parametrize("error_type", ["ParserConfigurationError", "GraphBuildConfigurationError"])
def test_legacy_start_hides_every_other_configuration_error_text(
    client: TestClient,
    service: MockAppService,
    monkeypatch: pytest.MonkeyPatch,
    error_type: str,
) -> None:
    async def refuse(**_kwargs: object) -> AppResponse:
        return AppResponse(
            False,
            {"error_type": error_type},
            "Parser catalog /srv/harborrag/sentinel-detail.yaml is invalid",
        )

    monkeypatch.setattr(service, "start_ingestion", refuse)
    response = client.post(
        "/api/v1/ingestions",
        json={"tenant_id": "DEFAULT", "connector_name": "x"},
    )

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "configuration_unavailable"
    assert error["message"] == "Configuration is unavailable"
    assert "sentinel-detail" not in response.text


def test_v1_submit_never_echoes_a_parser_configuration_error(
    service: MockAppService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harborrag_runtime.config.errors import ParserConfigurationError

    async def refuse(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ParserConfigurationError("Parser catalog /srv/harborrag/sentinel-detail.yaml")

    monkeypatch.setattr(service, "submit", refuse)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings()), raise_server_exceptions=False) as client:
        response = client.post("/v1/ingestions", json={"connection_id": "harborrag-workspace"})

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Internal server error"
    assert "sentinel-detail" not in response.text
