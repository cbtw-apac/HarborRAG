"""Local authenticated HTTP presentation for the HarborRAG MCP server."""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Sequence
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from harborrag_core.contracts.errors import HarborAuthorizationUnavailableError
from harborrag_mcp_server.configuration import (
    ConfigurationReadOnlyError,
    ConfigurationRevisionError,
    McpConfiguration,
    McpConfigurationStore,
)
from harborrag_mcp_server.server.http_auth import (
    ADMIN_SCOPE,
    authorize_administration,
    authorize_request_tenant,
    granted_tenants,
    grants_any_tenant,
    owner_only,
    reader_or_owner,
    request_tenant_default,
)
from harborrag_mcp_server.server.http_responses import (
    browser_security_headers,
    configuration_response,
    error_response,
)
from harborrag_mcp_server.server.server import is_caller_correctable

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.server.auth import TokenVerifier
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response

    from harborrag_mcp_server.server.server import McpServer

logger = logging.getLogger("harborrag.mcp.server.http")

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REQUIRED_SCOPE = "mcp:read"
_MAX_CONFIGURATION_REQUEST_BYTES = 1024 * 1024
_MAX_TOOL_REQUEST_BYTES = 128 * 1024
_STATUS_TEMPLATE = (Path(__file__).parent / "static" / "status.html").read_text(encoding="utf-8")
_PLACEHOLDER = re.compile(r"__MCP_(?:ENDPOINT|TOOLS|NONCE)__")


def validate_local_http_settings(
    *,
    host: str,
    port: int,
    path: str,
    bearer_token: str | None,
    allowed_hosts: Sequence[str] = (),
) -> str:
    """Validate the deliberately local-only development HTTP boundary."""
    validate_http_bind(host=host, port=port, path=path, allowed_hosts=allowed_hosts)
    return validate_bearer_token(bearer_token)


def validate_bearer_token(bearer_token: str | None) -> str:
    """Return the static owner token, refusing one too short to resist guessing."""
    token = (bearer_token or "").strip()
    if len(token.encode("utf-8")) < 32:
        raise ValueError("HARBORRAG_MCP_BEARER_TOKEN must contain at least 32 UTF-8 bytes")
    return token


def parse_allowed_hosts(value: str | None) -> tuple[str, ...]:
    """Split ``HARBORRAG_MCP_ALLOWED_HOSTS`` into the host names it lists."""
    return tuple(name.strip() for name in (value or "").split(",") if name.strip())


def require_loopback_host_names(allowed_hosts: Sequence[str]) -> None:
    """Keep the static owner token under host names only this machine uses.

    Args:
        allowed_hosts: The host names ``HARBORRAG_MCP_ALLOWED_HOSTS`` lists.

    Raises:
        ValueError: A listed host is not ``127.0.0.1``, ``localhost`` or ``::1``.
    """
    remote = sorted(set(allowed_hosts) - _LOCAL_HOSTS)
    if remote:
        raise ValueError(
            "HARBORRAG_MCP_ALLOWED_HOSTS may list only 127.0.0.1, localhost or ::1 while the "
            "static owner token is the only credential; set HARBORRAG_MCP_AUTH_MODE=api_key "
            f"to serve {', '.join(remote)}"
        )


def require_acknowledged_bind(host: str, *, environment: str, acknowledgement: str | None) -> None:
    """Refuse a bind beyond loopback that a development deployment has not acknowledged.

    Beyond loopback, tokens travel over plain HTTP to whoever can reach the port.
    The Host header check does not stop such a client, because the client sets
    that header itself. The acknowledgement is meant for a container whose runtime
    publishes the port on loopback only.

    Args:
        host: The address the server is asked to bind.
        environment: The deployment environment, ``dev``, ``staging`` or ``prod``.
        acknowledgement: The value of ``HARBORRAG_MCP_ALLOW_INSECURE_BIND``.

    Raises:
        ValueError: The host is not loopback and the bind is not acknowledged in ``dev``.
    """
    if host in _LOCAL_HOSTS:
        return
    if environment == "dev" and (acknowledgement or "").strip().lower() == "true":
        return
    raise ValueError(
        "MCP HTTP may bind beyond 127.0.0.1, localhost or ::1 only with HARBORRAG_ENV=dev "
        "and HARBORRAG_MCP_ALLOW_INSECURE_BIND=true, for a container whose port is published "
        "on loopback; beyond loopback, tokens are sent over plain HTTP"
    )


def served_origins(allowed_hosts: Sequence[str]) -> tuple[str, ...]:
    """Return the browser origins of the named hosts that are not loopback.

    A page served under such a host through a TLS proxy sends an ``https`` origin
    while this server sees plain HTTP, so both schemes are trusted, on any port
    unless the name carries one.

    Args:
        allowed_hosts: The host names ``HARBORRAG_MCP_ALLOWED_HOSTS`` lists.

    Returns:
        One origin pattern per scheme for each host.
    """
    return tuple(
        f"{scheme}://{host}" if ":" in host else f"{scheme}://{host}:*"
        for host in allowed_hosts
        if host not in _LOCAL_HOSTS
        for scheme in ("http", "https")
    )


def validate_http_bind(
    *, host: str, port: int, path: str, allowed_hosts: Sequence[str] = ()
) -> None:
    """Require loopback serving unless the hosts that clients use are named.

    A container listens on every interface while its runtime publishes the port, so
    it names the hosts it serves, and the Host header check turns away browsers
    sent there under another name. ``require_acknowledged_bind`` decides whether
    such a bind is accepted at all. Remote deployments still terminate TLS at a
    proxy.
    """
    if host not in _LOCAL_HOSTS and not allowed_hosts:
        raise ValueError(
            "local MCP HTTP may bind only to 127.0.0.1, localhost, or ::1 unless "
            "HARBORRAG_MCP_ALLOWED_HOSTS names the hosts clients use; "
            "use a TLS reverse proxy and production token verifier for remote access"
        )
    if not 1 <= port <= 65_535:
        raise ValueError("MCP HTTP port must be between 1 and 65535")
    if not path.startswith("/") or path == "/" or path.endswith("/"):
        raise ValueError(
            "MCP HTTP path must start with '/', must not be '/', and must not end with '/'"
        )


def create_local_token_verifier(token: str, *, tenant_id: str = "*") -> TokenVerifier:
    """Create the explicit static-token verifier used only on loopback.

    The owner administers this server whichever tenant its reads are bound to, so
    only the tenant list narrows with ``tenant_id``.
    """
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    return cast(
        "TokenVerifier",
        StaticTokenVerifier(
            tokens={
                token: {
                    "client_id": "harborrag-local",
                    "sub": "harborrag-local",
                    "role": "owner",
                    "tenants": [tenant_id],
                    "scopes": [_REQUIRED_SCOPE, ADMIN_SCOPE],
                }
            },
            required_scopes=[_REQUIRED_SCOPE],
        ),
    )


def create_api_key_owner_verifier(
    *,
    bearer_token: str | None,
    tenant_id: str | None,
) -> TokenVerifier | None:
    """The owner token to accept beside reader keys, or ``None`` to accept none.

    ``api_key`` mode exists to serve remote callers through a TLS proxy, so the
    loopback bind no longer limits who can present the static owner token. That
    token never expires and cannot be revoked, so it is accepted only when
    ``HARBORRAG_MCP_READER_TENANT_ID`` binds its reads to one concrete tenant;
    the ``*`` default would let it read every tenant. Without that binding the
    server starts with reader keys only and says how to enable the owner.
    """
    token = (bearer_token or "").strip()
    if not token:
        return None
    tenant = (tenant_id or "").strip()
    if not tenant or tenant == "*":
        logger.warning(
            "HARBORRAG_MCP_BEARER_TOKEN is ignored in api_key mode: the owner token is "
            "accepted beside reader keys only when HARBORRAG_MCP_READER_TENANT_ID names "
            "one tenant (not '*'). Set HARBORRAG_MCP_READER_TENANT_ID=<tenant> and "
            "restart to enable the status UI's configuration editor."
        )
        return None
    return create_local_token_verifier(validate_bearer_token(token), tenant_id=tenant)


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


def register_health_route(
    server: FastMCP[Any],
    *,
    mcp_path: str,
    tool_names: list[str],
    service: str,
) -> None:
    """Attach only the unauthenticated health route, for the Explorer UI server."""
    server.custom_route("/healthz", methods=["GET"], include_in_schema=False)(
        _health_handler(mcp_path, tool_names, service=service)
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
    *,
    service: str = "harborrag-mcp",
) -> Callable[[Request], Awaitable[Response]]:
    from starlette.responses import JSONResponse

    async def health(request: Request) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "status": "ok",
                "service": service,
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
        authorize_administration(request)
        return configuration_response(configuration.describe())

    return get_configuration


def _replace_configuration_handler(
    configuration: McpConfigurationStore,
    token_verifier: TokenVerifier,
) -> Callable[[Request], Awaitable[Response]]:
    @owner_only(token_verifier)
    async def replace_configuration(request: Request, principal_id: str) -> JSONResponse:
        try:
            authorize_administration(request)
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
        except (ConfigurationRevisionError, ConfigurationReadOnlyError) as exc:
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
            authorize_administration(request)
            description = configuration.reload(principal_id=principal_id)
        except (ValidationError, ValueError) as exc:
            return error_response(str(exc), status_code=422)
        return configuration_response(description)

    return reload_configuration


def _list_tools_handler(
    registry: McpServer,
    token_verifier: TokenVerifier,
) -> Callable[[Request], Awaitable[Response]]:
    """Build the route that lists a caller's tool catalog.

    A reader sees its own tenant's catalog, and naming another tenant is refused.
    A token bound to one tenant gets that tenant when the request names none. The
    response carries the caller's role, so the page can say that configuration
    needs the owner token. A request that names no tenant also gets the tenants
    the caller may choose. A token that names tenants gets those. A token for any
    tenant gets the tenants that hold content and the ones the server was started
    with.
    """

    @reader_or_owner(token_verifier)
    async def list_tools(request: Request, principal_id: str) -> JSONResponse:
        tenant_value = request.query_params.get("tenant_id")
        tenant_id = tenant_value.strip() if tenant_value is not None else None
        if tenant_value is not None and not tenant_id:
            return error_response("tenant_id must not be empty", status_code=422)
        if tenant_id is None:
            tenant_id = request_tenant_default(request)
        if tenant_id is None:
            authorize_administration(request)
        else:
            authorize_request_tenant(request, tenant_id)
        tools = [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
                "capability": spec.capability,
            }
            for spec in registry.list_tools(tenant_id)
        ]
        catalog: dict[str, object] = {
            "tenant_id": tenant_id,
            "tools": tools,
            "role": getattr(request.state, "token_role", "owner"),
        }
        if tenant_value is None:
            any_tenant = grants_any_tenant(request)
            catalog["any_tenant"] = any_tenant
            catalog["tenants"] = (
                await registry.tenant_choices() if any_tenant else granted_tenants(request)
            )
        return configuration_response(catalog)

    return list_tools


def _call_tool_handler(
    registry: McpServer,
    token_verifier: TokenVerifier,
) -> Callable[[Request], Awaitable[Response]]:
    """Build the route that runs one tool for the Tool Playground.

    Failures are reported the way the MCP transport reports them. A malformed
    request and a refusal ``call_tool`` raises before the tool runs are the
    caller's to correct, so they are answered in full with 422 or 403. A failure
    inside the tool is logged with its traceback and answered with its exception
    type only. A cross-tenant call raises ``Unauthorized``, which the route guard
    answers as an authorization decision and not as a server fault.
    """

    @reader_or_owner(token_verifier)
    async def call_tool(request: Request, principal_id: str) -> JSONResponse:
        try:
            name, arguments = await _tool_call(request)
        except (ValueError, RecursionError) as exc:
            return error_response(str(exc), status_code=422)
        tenant_id = _bind_tenant(request, registry, name, arguments)
        if isinstance(tenant_id, str):
            authorize_request_tenant(request, tenant_id)
        try:
            result = await registry.call_tool(name, arguments, principal_id=principal_id)
        except HarborAuthorizationUnavailableError:
            return error_response("AUTHORIZATION_UNAVAILABLE", status_code=503)
        except Exception as exc:
            if is_caller_correctable(exc):
                refused = 403 if isinstance(exc, PermissionError) else 422
                return error_response(str(exc), status_code=refused)
            logger.exception("mcp_playground_tool_failed")
            return error_response(
                f"tool execution failed ({type(exc).__name__})",
                status_code=500,
            )
        return configuration_response({"name": name, "result": result})

    return call_tool


async def _tool_call(request: Request) -> tuple[str, dict[str, object]]:
    """Read the tool name and its arguments from a playground request body.

    Args:
        request: The playground's request to run a tool.

    Returns:
        The tool's name without surrounding whitespace, and its arguments.

    Raises:
        ValueError: The body is not the JSON object the playground sends, or it
            is over the request budget.
    """
    payload = await _bounded_json(request, maximum=_MAX_TOOL_REQUEST_BYTES)
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    name = payload.get("name")
    arguments = payload.get("arguments")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    return name.strip(), arguments


def _bind_tenant(
    request: Request,
    registry: McpServer,
    tool_name: str,
    arguments: dict[str, object],
) -> object:
    """Return the call's tenant, defaulting a tenantless call to the token's own.

    This is the binding the MCP transport already applies: a token that grants one
    tenant supplies it. Left unset, the call fell through to the ``local`` tenant
    under source ACLs, so the playground answered every query with an empty corpus.

    Fail closed on the schema: the tenant is only defaulted for a tool that takes
    one, read from the registered catalog rather than the configured view -- a tool
    disabled for this tenant declares the same schema, and refusing it is
    ``call_tool``'s decision to make.
    """
    tenant_id = arguments.get("tenant_id")
    tool = next((item for item in registry.tools or () if item.spec.name == tool_name), None)
    if tenant_id is not None or tool is None:
        return tenant_id
    properties = tool.spec.input_schema.get("properties")
    if not isinstance(properties, dict) or "tenant_id" not in properties:
        return None
    bound = request_tenant_default(request)
    if bound is not None:
        arguments["tenant_id"] = bound
    return bound


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
    """Render the operator status page from its standalone HTML template.

    The template is a real ``.html`` file, so its markup, CSS and browser
    JavaScript stay lintable. Placeholders are replaced in one pass, so a
    substituted value that contains another placeholder is not rewritten.
    """
    substitutions = {
        "__MCP_ENDPOINT__": escape(mcp_path),
        "__MCP_TOOLS__": "".join(f"<li><code>{escape(name)}</code></li>" for name in tool_names),
        "__MCP_NONCE__": escape(nonce, quote=True),
    }
    return _PLACEHOLDER.sub(lambda match: substitutions[match.group(0)], _STATUS_TEMPLATE)
