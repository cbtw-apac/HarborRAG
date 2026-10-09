"""HTTP tool calls answer with reviewed messages, never raw exception text."""

from __future__ import annotations

import errno
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette

from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfiguration, McpConfigurationStore
from harborrag_mcp_server.server import McpServer, create_mcp_server
from harborrag_mcp_server.server.http import create_local_token_verifier, register_http_routes
from harborrag_mcp_server.server.tool_errors import ToolRequestError, public_tool_error

TOKEN = "local-test-token-that-is-at-least-32-bytes"
SENTINEL = "/srv/harborrag/sentinel-secret-path"


class _Arguments(BaseModel):
    top_k: int


def _validation_error() -> ValidationError:
    try:
        _Arguments.model_validate({"top_k": SENTINEL})
    except ValidationError as error:
        return error
    raise AssertionError("expected a validation error")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ToolRequestError("arguments must be an object"), ("arguments must be an object", 422)),
        (_validation_error(), ("Invalid input for field(s): top_k", 422)),
        (
            PermissionError("MCP tool vector_search is disabled"),
            ("MCP tool vector_search is disabled", 403),
        ),
        (PermissionError(errno.EACCES, "Permission denied", SENTINEL), ("permission denied", 403)),
        (ValueError(f"bad catalog at {SENTINEL}"), ("invalid tool request (ValueError)", 422)),
        (TypeError(SENTINEL), ("invalid tool request (TypeError)", 422)),
        (RuntimeError(SENTINEL), ("tool execution failed (RuntimeError)", 500)),
    ],
)
def test_public_tool_error_never_echoes_unreviewed_text(
    exc: Exception, expected: tuple[str, int]
) -> None:
    assert public_tool_error(exc) == expected


def _app(tmp_path: Path) -> Starlette:
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(audit=audit)
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration(),
        specs=registry.list_tools(),
        audit=audit,
        environment={},
    )
    registry.configuration = configuration
    verifier = create_local_token_verifier(TOKEN)
    server = create_mcp_server(registry=registry, auth=verifier)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        token_verifier=verifier,
    )
    return server.http_app(path="/mcp")


async def _call(app: Starlette, body: dict[str, object]) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                "/api/tools/call", headers={"Authorization": f"Bearer {TOKEN}"}, json=body
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "status"),
    [
        (ValueError(SENTINEL), 422),
        (PermissionError(errno.EACCES, "Permission denied", SENTINEL), 403),
        (RuntimeError(SENTINEL), 500),
    ],
)
async def test_http_tool_call_hides_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception, status: int
) -> None:
    async def explode(self: McpServer, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise raised

    monkeypatch.setattr(McpServer, "call_tool", explode)

    response = await _call(
        _app(tmp_path),
        {"name": "vector_search", "arguments": {"query": "harbor", "tenant_id": "demo"}},
    )

    assert response.status_code == status
    assert SENTINEL not in response.text


@pytest.mark.asyncio
async def test_an_unknown_tool_is_a_fixed_422(tmp_path: Path) -> None:
    response = await _call(_app(tmp_path), {"name": "no_such_tool", "arguments": {}})

    assert response.status_code == 422
    assert response.json()["error"] == "unknown tool"


@pytest.mark.asyncio
async def test_http_tool_call_reports_validation_error_fields_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _validation_error()

    async def reject(self: McpServer, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise error

    monkeypatch.setattr(McpServer, "call_tool", reject)

    response = await _call(
        _app(tmp_path),
        {"name": "vector_search", "arguments": {"query": SENTINEL, "tenant_id": "demo"}},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "Invalid input for field(s): top_k"
    assert SENTINEL not in response.text


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_not_echoed(tmp_path: Path) -> None:
    response = await _call(_app(tmp_path), {"name": SENTINEL, "arguments": {}})

    assert response.status_code == 422
    assert SENTINEL not in response.text


async def _config_request(
    app: Starlette, method: str, path: str, body: dict[str, object] | None = None
) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.request(
                method, path, headers={"Authorization": f"Bearer {TOKEN}"}, json=body
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configuration",
    [
        {"tools": {SENTINEL: {}}},
        {"tenants": {"demo": {"tools": {"vector_search": {"defaults": {SENTINEL: 1}}}}}},
        {"tools": {"vector_search": {"enabled": SENTINEL}}},
        {"unexpected_field": SENTINEL},
    ],
)
async def test_rejected_configuration_never_echoes_submitted_values(
    tmp_path: Path, configuration: dict[str, object]
) -> None:
    response = await _config_request(
        _app(tmp_path), "PUT", "/api/config", {"configuration": configuration}
    )

    assert response.status_code == 422
    assert SENTINEL not in response.text


@pytest.mark.asyncio
async def test_configuration_request_shape_errors_keep_their_message(tmp_path: Path) -> None:
    response = await _config_request(_app(tmp_path), "PUT", "/api/config", {"configuration": 1})

    assert response.status_code == 422
    assert response.json()["error"] == "request must contain a configuration object"


@pytest.mark.asyncio
async def test_a_failed_reload_does_not_name_the_configuration_path(tmp_path: Path) -> None:
    response = await _config_request(_app(tmp_path), "POST", "/api/config/reload")

    assert response.status_code == 422
    assert str(tmp_path) not in response.text
    assert response.json()["error"] == "invalid configuration (ValueError)"
