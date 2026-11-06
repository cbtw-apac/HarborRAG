"""The exported OpenAPI schema is valid, stable JSON with the v2.0.0 surface."""

from __future__ import annotations

import json
from typing import Any

import pytest

from harborrag_app import __version__
from harborrag_app.api.export_openapi import export_openapi

_ERROR_REF = {"$ref": "#/components/schemas/Error"}
_STANDARD_ERRORS = {"401", "403", "429", "500", "503", "504"}


@pytest.mark.blackbox
def test_export_produces_stable_schema_with_the_v2_surface() -> None:
    """Schema parses, carries the title/version, exactly the v2.0.0 routes, and the
    bearer security scheme; two exports are byte-identical (diffable in CI)."""
    rendered = export_openapi()
    schema = json.loads(rendered)
    assert schema["info"]["title"] == "HarborRAG Control Plane API"
    assert schema["info"]["version"] == __version__
    paths = schema["paths"]
    assert set(paths) == {
        "/healthz",
        "/readyz",
        "/v1/admin/connections",
        "/v1/admin/ingestions",
        "/v1/admin/ingestions/{ingestion_id}",
        "/v1/admin/ingestions/{ingestion_id}/cancel",
        "/v1/admin/ingestions/{ingestion_id}/documents",
        "/v1/admin/ingestions/{ingestion_id}/events",
        "/v1/admin/ingestions/{ingestion_id}/pause",
        "/v1/admin/ingestions/{ingestion_id}/resume",
        "/v1/admin/ingestions/{ingestion_id}/retry-failures",
        "/v1/admin/projects",
        "/v1/admin/projects/{project_id}",
        "/v1/admin/projects/{project_id}/connections/{connection_id}",
        "/v1/admin/projects/{project_id}/connections/{connection_id}/test",
        "/v1/admin/source-types",
        "/v1/chat/completions",
        "/v1/chat/sessions",
        "/v1/chat/sessions/{session_id}/messages",
        "/v1/me",
        "/v1/projects/{project_id}/connections",
    }
    assert set(paths["/v1/chat/completions"]) == {"post"}
    assert set(paths["/v1/chat/sessions"]) == {"get", "post"}
    assert set(paths["/v1/chat/sessions/{session_id}/messages"]) == {"get"}
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert rendered == export_openapi()


@pytest.mark.blackbox
def test_chat_request_examples_are_sendable_as_written() -> None:
    """The docs schema example must be a body a reader can paste and send.

    Every optional field on these requests carries a pattern, so without an
    explicit example the schema generator invents a value for each one. Those
    invented values look plausible and are not: a generated ``project_id`` or
    ``model`` names something that does not exist, so the pasted body fails
    with a 404 or a 422 and the reader blames the endpoint.
    """

    schema = json.loads(export_openapi())
    components = schema["components"]["schemas"]
    # Optional fields whose generated values would name absent resources.
    unsendable = {"project_id", "model"}

    for name in ("ChatCompletionRequest", "ChatSessionCreateRequest"):
        examples = components[name].get("examples")
        assert examples, f"{name} must publish a request example"
        for example in examples:
            assert not unsendable & set(example), (
                f"{name} example must omit {unsendable & set(example)}: "
                "a generated value there names a resource that does not exist"
            )
            for field, value in example.items():
                assert field in components[name]["properties"], (
                    f"{name} example sets unknown field {field!r}; the request "
                    "forbids extras, so the published example would be rejected"
                )
                if isinstance(value, str):
                    assert value.strip(), f"{name} example field {field!r} is blank"

    # The required fields must actually be present, or the example cannot be sent.
    for name, required in (("ChatCompletionRequest", {"prompt"}),):
        example = components[name]["examples"][0]
        assert required <= set(example), f"{name} example omits {required - set(example)}"

    chat_request = components["ChatCompletionRequest"]
    assert "session_id" in chat_request["properties"]

    body = schema["paths"]["/v1/chat/completions"]["post"]["requestBody"]
    examples = body["content"]["application/json"]["examples"]
    assert next(iter(examples)) == "existing_session"
    assert examples["existing_session"]["value"]["session_id"].startswith("session-")
    assert "session_id" not in examples["new_session"]["value"]


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    return json.loads(export_openapi())


def test_every_operation_declares_its_role(openapi: dict[str, Any]) -> None:
    for path, item in openapi["paths"].items():
        if path.startswith("/v1/"):
            for method, operation in item.items():
                assert operation.get("x-min-role") in {"reader", "editor", "admin", "owner"}, (
                    method,
                    path,
                )


def test_every_error_response_uses_the_shared_error_schema(openapi: dict[str, Any]) -> None:
    for path, item in openapi["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for method, operation in item.items():
            responses = operation["responses"]
            assert _STANDARD_ERRORS <= set(responses), (method, path)
            for status, response in responses.items():
                if int(status) >= 400:
                    schema = response["content"]["application/json"]["schema"]
                    assert schema == _ERROR_REF, (method, path, status)


def test_framework_validation_schemas_are_gone(openapi: dict[str, Any]) -> None:
    schemas = openapi["components"]["schemas"]

    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas
    assert set(schemas["Error"]["required"]) == {"error"}
