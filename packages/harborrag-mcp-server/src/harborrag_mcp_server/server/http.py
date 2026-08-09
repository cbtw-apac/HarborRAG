"""Local authenticated HTTP presentation for the HarborRAG MCP server."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from harborrag_mcp_server.configuration import (
    ConfigurationRevisionError,
    McpConfiguration,
    McpConfigurationStore,
)
from harborrag_mcp_server.server.http_auth import authorize_request_tenant, owner_only
from harborrag_mcp_server.server.http_responses import (
    browser_security_headers,
    configuration_response,
    error_response,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.server.auth import TokenVerifier
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response

    from harborrag_mcp_server.server.server import McpServer

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REQUIRED_SCOPE = "mcp:read"
_MAX_CONFIGURATION_REQUEST_BYTES = 1024 * 1024
_MAX_TOOL_REQUEST_BYTES = 128 * 1024
# Kept as a real .html file so the markup, CSS, and browser JS stay lintable and
# formattable instead of living in a Python f-string with every brace doubled.
_STATUS_TEMPLATE = (Path(__file__).parent / "static" / "status.html").read_text(encoding="utf-8")
_PLACEHOLDER = re.compile(r"__MCP_(?:ENDPOINT|TOOLS|NONCE)__")


def validate_local_http_settings(
    *,
    host: str,
    port: int,
    path: str,
    bearer_token: str | None,
) -> str:
    """Validate the deliberately local-only development HTTP boundary."""
    if host not in _LOCAL_HOSTS:
        raise ValueError(
            "local MCP HTTP may bind only to 127.0.0.1, localhost, or ::1; "
            "use a TLS reverse proxy and production token verifier for remote access"
        )
    if not 1 <= port <= 65_535:
        raise ValueError("MCP HTTP port must be between 1 and 65535")
    if not path.startswith("/") or path == "/" or path.endswith("/"):
        raise ValueError(
            "MCP HTTP path must start with '/', must not be '/', and must not end with '/'"
        )
    token = (bearer_token or "").strip()
    if len(token.encode("utf-8")) < 32:
        raise ValueError("HARBORRAG_MCP_BEARER_TOKEN must contain at least 32 UTF-8 bytes")
    return token


def create_local_token_verifier(token: str) -> TokenVerifier:
    """Create the explicit static-token verifier used only on loopback."""
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    return cast(
        "TokenVerifier",
        StaticTokenVerifier(
            tokens={
                token: {
                    "client_id": "harborrag-local",
                    "sub": "harborrag-local",
                    "role": "owner",
                    "tenants": ["*"],
                    "scopes": [_REQUIRED_SCOPE],
                }
            },
            required_scopes=[_REQUIRED_SCOPE],
        ),
    )


def register_http_routes(
    server: FastMCP[Any],
    *,
    mcp_path: str,
    registry: McpServer,
    configuration: McpConfigurationStore,
    token_verifier: TokenVerifier,
) -> None:
    """Attach human-readable status and machine-readable health routes."""
    tool_names = [spec.name for spec in registry.list_tools()]
    server.custom_route("/", methods=["GET"], include_in_schema=False)(
        _status_handler(mcp_path, tool_names)
    )
    server.custom_route("/healthz", methods=["GET"], include_in_schema=False)(
        _health_handler(mcp_path, tool_names)
    )
    server.custom_route("/api/config", methods=["GET"], include_in_schema=False)(
        _get_configuration_handler(configuration, token_verifier)
    )
    server.custom_route("/api/config", methods=["PUT"], include_in_schema=False)(
        _replace_configuration_handler(configuration, token_verifier)
    )
    server.custom_route("/api/config/reload", methods=["POST"], include_in_schema=False)(
        _reload_configuration_handler(configuration, token_verifier)
    )
    server.custom_route("/api/tools", methods=["GET"], include_in_schema=False)(
        _list_tools_handler(registry, token_verifier)
    )
    server.custom_route("/api/tools/call", methods=["POST"], include_in_schema=False)(
        _call_tool_handler(registry, token_verifier)
    )


def _status_handler(
    mcp_path: str,
    tool_names: list[str],
) -> Callable[[Request], Awaitable[Response]]:
    from starlette.responses import HTMLResponse

    async def status_page(request: Request) -> HTMLResponse:
        del request
        nonce = secrets.token_urlsafe(18)
        return HTMLResponse(
            _status_page(mcp_path=mcp_path, tool_names=tool_names, nonce=nonce),
            headers=browser_security_headers(nonce),
        )

    return status_page


def _health_handler(
    mcp_path: str,
    tool_names: list[str],
) -> Callable[[Request], Awaitable[Response]]:
    from starlette.responses import JSONResponse

    async def health(request: Request) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "status": "ok",
                "service": "harborrag-mcp",
                "transport": "streamable-http",
                "mcp_path": mcp_path,
                "authentication": "bearer",
                "tool_count": len(tool_names),
            },
            headers={"Cache-Control": "no-store"},
        )

    return health


def _get_configuration_handler(
    configuration: McpConfigurationStore,
    token_verifier: TokenVerifier,
) -> Callable[[Request], Awaitable[Response]]:
    @owner_only(token_verifier)
    async def get_configuration(request: Request, principal_id: str) -> JSONResponse:
        authorize_request_tenant(request, "*")
        return configuration_response(configuration.describe())

    return get_configuration


def _replace_configuration_handler(
    configuration: McpConfigurationStore,
    token_verifier: TokenVerifier,
) -> Callable[[Request], Awaitable[Response]]:
    @owner_only(token_verifier)
    async def replace_configuration(request: Request, principal_id: str) -> JSONResponse:
        try:
            authorize_request_tenant(request, "*")
            payload = await _bounded_json(request, maximum=_MAX_CONFIGURATION_REQUEST_BYTES)
            if not isinstance(payload, dict) or not isinstance(payload.get("configuration"), dict):
                raise ValueError("request must contain a configuration object")
            expected_revision = payload.get("expected_revision")
            if expected_revision is not None and not isinstance(expected_revision, str):
                raise ValueError("expected_revision must be a string")
            updated = McpConfiguration.model_validate(payload["configuration"])
            description = configuration.replace(
                updated,
                principal_id=principal_id,
                expected_revision=expected_revision,
            )
        except ConfigurationRevisionError as exc:
            return error_response(str(exc), status_code=409)
        except (ValidationError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return error_response(str(exc), status_code=422)
        return configuration_response(description)

    return replace_configuration


def _reload_configuration_handler(
    configuration: McpConfigurationStore,
    token_verifier: TokenVerifier,
) -> Callable[[Request], Awaitable[Response]]:
    @owner_only(token_verifier)
    async def reload_configuration(request: Request, principal_id: str) -> JSONResponse:
        try:
            authorize_request_tenant(request, "*")
            description = configuration.reload(principal_id=principal_id)
        except (ValidationError, ValueError) as exc:
            return error_response(str(exc), status_code=422)
        return configuration_response(description)

    return reload_configuration


def _list_tools_handler(
    registry: McpServer,
    token_verifier: TokenVerifier,
) -> Callable[[Request], Awaitable[Response]]:
    @owner_only(token_verifier)
    async def list_tools(request: Request, principal_id: str) -> JSONResponse:
        tenant_value = request.query_params.get("tenant_id")
        tenant_id = tenant_value.strip() if tenant_value is not None else None
        if tenant_value is not None and not tenant_id:
            return error_response("tenant_id must not be empty", status_code=422)
        authorize_request_tenant(request, tenant_id or "*")
        tools = [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
                "capability": spec.capability,
            }
            for spec in registry.list_tools(tenant_id)
        ]
        return configuration_response({"tenant_id": tenant_id, "tools": tools})

    return list_tools


def _call_tool_handler(
    registry: McpServer,
    token_verifier: TokenVerifier,
) -> Callable[[Request], Awaitable[Response]]:
    @owner_only(token_verifier)
    async def call_tool(request: Request, principal_id: str) -> JSONResponse:
        try:
            payload = await _bounded_json(request, maximum=_MAX_TOOL_REQUEST_BYTES)
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            name = payload.get("name")
            arguments = payload.get("arguments")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("name must be a non-empty string")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            tenant_id = arguments.get("tenant_id")
            if isinstance(tenant_id, str):
                authorize_request_tenant(request, tenant_id)
            result = await registry.call_tool(
                name.strip(),
                arguments,
                principal_id=principal_id,
            )
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return error_response(str(exc), status_code=422)
        except Exception as exc:
            return error_response(
                f"tool execution failed ({type(exc).__name__})",
                status_code=500,
            )
        return configuration_response({"name": name.strip(), "result": result})

    return call_tool


async def _bounded_json(request: Request, *, maximum: int) -> object:
    """Read a JSON body without allocating beyond the HTTP boundary budget."""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if declared < 0 or declared > maximum:
            raise ValueError("request body budget exceeded")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise ValueError("request body budget exceeded")
        body.extend(chunk)
    return json.loads(body)


def _status_page(*, mcp_path: str, tool_names: list[str], nonce: str) -> str:
    """Render the operator status page from its standalone HTML template."""

    substitutions = {
        "__MCP_ENDPOINT__": escape(mcp_path),
        "__MCP_TOOLS__": "".join(f"<li><code>{escape(name)}</code></li>" for name in tool_names),
        "__MCP_NONCE__": escape(nonce, quote=True),
    }
    # One pass, so a substituted value that happens to contain another placeholder is not
    # itself rewritten by a later replace().
    return _PLACEHOLDER.sub(lambda match: substitutions[match.group(0)], _STATUS_TEMPLATE)


__all__ = [
    "create_local_token_verifier",
    "register_http_routes",
    "validate_local_http_settings",
]
