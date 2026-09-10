"""The exported OpenAPI schema is valid, stable JSON with the M0 surface (ST10)."""

from __future__ import annotations

import json

import pytest

from harborrag_app import __version__
from harborrag_app.api.export_openapi import export_openapi


@pytest.mark.blackbox
def test_export_produces_stable_schema_with_m0_surface() -> None:
    """Schema parses, carries the title/version, the M0 routes, and the
    bearer security scheme; two exports are byte-identical (diffable in CI)."""
    rendered = export_openapi()
    schema = json.loads(rendered)
    assert schema["info"]["title"] == "HarborRAG Control Plane API"
    assert schema["info"]["version"] == __version__
    paths = schema["paths"]
    assert {
        "/api/v1/metrics",
        "/api/v1/health",
        "/api/v1/readyz",
        "/api/v1/diagnostics",
        "/api/v1/ingestions",
        "/api/v1/ingestions/{run_id}",
        "/api/v1/ingestions/{run_id}/actions",
        "/api/v1/ingestions/{run_id}/result",
        "/v1/ingestions",
        "/v1/ingestions/{task_id}",
        "/v1/ingestions/{task_id}/documents",
        "/v1/ingestions/{task_id}/cancel",
        "/v1/ingestions/{task_id}/retry-failures",
        "/v1/connections",
        "/v1/chat/completions",
        "/v1/chat/sessions",
        "/v1/chat/conversations",
        "/v1/chat/conversations/{session_id}",
        "/v1/chat/conversations/{session_id}/messages",
        "/v1/agent/completions",
        "/v1/agent/sessions",
        "/v1/retrieval/vector",
        "/v1/retrieval/graph/triplets",
        "/v1/retrieval/graph/paths",
        "/v1/retrieval/graph/subgraphs",
        "/v1/memory/memories",
        "/v1/memory/memories/{memory_id}",
        "/v1/memory/sessions/{session_id}",
        "/v1/memory/users/{user_id}",
        "/v1/admin/projections/{tenant}",
    } <= set(paths)
    assert set(paths["/v1/ingestions"]) >= {"get", "post"}
    assert set(paths["/v1/connections"]) == {"get"}
    assert set(paths["/v1/chat/completions"]) >= {"post"}
    assert "get" not in paths["/v1/chat/completions"]
    assert set(paths["/v1/agent/completions"]) >= {"post"}
    assert "get" not in paths["/v1/agent/completions"]
    assert "/v1/retrieval/search" not in paths
    assert set(paths["/v1/chat/conversations"]) == {"get"}
    assert set(paths["/v1/chat/conversations/{session_id}"]) == {"patch", "delete"}
    assert set(paths["/v1/chat/conversations/{session_id}/messages"]) == {"get"}
    assert set(paths["/v1/memory/memories"]) == {"get"}
    assert set(paths["/v1/memory/users/{user_id}"]) == {"delete"}
    for path in (
        "/api/v1/diagnostics",
        "/api/v1/ingestions",
        "/api/v1/ingestions/{run_id}",
        "/api/v1/ingestions/{run_id}/actions",
        "/api/v1/ingestions/{run_id}/result",
    ):
        assert all(operation["deprecated"] is True for operation in paths[path].values())
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert rendered == export_openapi()


@pytest.mark.blackbox
def test_chat_and_agent_request_examples_are_sendable_as_written() -> None:
    """The docs example must be a body a reader can paste and have succeed.

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

    for name in (
        "ChatCompletionRequest",
        "AgentCompletionRequest",
        "AgentResumeRequest",
        "ChatSessionCreateRequest",
        "AgentSessionCreateRequest",
        "ConversationRenameRequest",
    ):
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
    for name, required in (
        ("ChatCompletionRequest", {"session_id", "prompt"}),
        ("AgentCompletionRequest", {"session_id", "prompt"}),
        ("AgentResumeRequest", {"session_id"}),
    ):
        example = components[name]["examples"][0]
        assert required <= set(example), f"{name} example omits {required - set(example)}"
