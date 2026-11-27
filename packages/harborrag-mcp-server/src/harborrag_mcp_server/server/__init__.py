"""FastMCP transports for the HarborRAG reader and Explorer servers.

Every tool call runs through one audited handler. Only a refusal the caller can
correct and a tool's own reported failure reach the client as text; the
transport masks any other exception to its type.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from harborrag_core.contracts.errors import HarborAuthorizationUnavailableError
from harborrag_core.contracts.tools import ToolInvocationContext, ToolSpec
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_mcp_server.server.base import BaseMcpServer, tool_reported_error
from harborrag_mcp_server.server.http_auth import authorize_claimed_tenant
from harborrag_mcp_server.server.server import McpServer, is_caller_correctable

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.server.auth import AccessToken, AuthProvider

    from harborrag_mcp_server.server.explorer import ToolHandler
    from harborrag_runtime.composition.readers import ReaderApplication

type _Lifespan = Callable[[object], AbstractAsyncContextManager[None]]

_SERVER_INSTRUCTIONS = (
    "Use list_sources when corpus scope is unclear. Find candidates with vector_search for "
    "natural-language questions (mode=local_semantic adds bounded graph expansion; "
    "include_content=false returns compact hits), or with find_entities when the question "
    "asks which issues, pages or other entities match, optionally by facet. Read and cite "
    "with fetch_evidence for chosen chunk IDs, including the evidence IDs find_entities "
    "returns, or get_document_context for an ordered, version-bound window of one "
    "document; list_documents pages the document inventory. To navigate relations, call "
    "resolve_graph_nodes when a provider ID or title may be ambiguous, then "
    "graph_triplet_search for exact relations, graph_subgraph_search for a neighborhood "
    "or graph_path_search between two nodes; describe_graph explains the graph model if "
    "you need orientation, and is never a required first call. Cite only evidence chunks "
    "returned with their text; graph records are navigation, not evidence. Preserve "
    "stored direction and report bounded completion reasons."
)
_AUTHORIZATION_UNAVAILABLE = (
    "AUTHORIZATION_UNAVAILABLE: The server could not verify data access. Retry later."
)
_REPORTED_FAILURE_FALLBACK = "tool execution failed"
_TRANSPORT_RELEASE_DELAY_S = 0.25
_LOCAL_PRINCIPAL = "local-unauthenticated"
_UNKNOWN_PRINCIPAL = "authenticated-unknown"
_TOOL_ROLES = frozenset({"reader", "owner"})


@dataclass(frozen=True, slots=True)
class _TransportOptions:
    """Options both transport factories share.

    Attributes:
        registry: Registry to serve; a default one is built over ``runtime``.
        runtime: Reader runtime whose invoker backs a default registry.
        auth: Authentication provider for network transports.
        allow_unauthenticated_local: Allow no ``auth``, for local stdio only.
        manage_runtime_lifecycle: Start and close ``runtime`` with the server.
    """

    registry: McpServer | None
    runtime: ReaderApplication | None
    auth: AuthProvider | None
    allow_unauthenticated_local: bool
    manage_runtime_lifecycle: bool


def list_tools() -> list[dict[str, object]]:
    """Describe each reader tool by name, description and input schema."""
    return [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema}
        for s in McpServer().list_tools()
    ]


async def call_tool(
    name: str, arguments: dict[str, object] | None = None, **kwargs: object
) -> dict[str, object]:
    """Call one reader tool in process, with keyword arguments merged into ``arguments``."""
    payload = dict(arguments or {})
    payload.update(kwargs)
    return await McpServer().call_tool(name, payload)


def create_mcp_server(
    *,
    registry: McpServer | None = None,
    runtime: ReaderApplication | None = None,
    auth: AuthProvider | None = None,
    allow_unauthenticated_local: bool = False,
    manage_runtime_lifecycle: bool = False,
) -> object:
    """Create the FastMCP reader server around the audited in-process registry.

    Tools are registered once, from the global configuration, because FastMCP
    builds its tool table at startup. A tool one tenant disables is still
    listed and is refused at call time with a message naming it. A tool
    disabled globally is not registered.

    Args:
        registry: Registry to serve; a default one is built over ``runtime``.
        runtime: Reader runtime whose invoker backs a default registry.
        auth: Authentication provider; required unless local stdio is allowed.
        allow_unauthenticated_local: Allow no ``auth``, for local stdio only.
        manage_runtime_lifecycle: Start and close ``runtime`` with the server.

    Returns:
        The FastMCP server, ready to run on a transport.

    Raises:
        RuntimeError: FastMCP is not installed, or ``auth`` is missing.
    """
    try:
        from fastmcp.tools import FunctionTool
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError(
            "FastMCP transport is not installed; install harborrag-mcp-server"
        ) from exc

    transport, facade = _transport(
        "HarborRAG",
        _SERVER_INSTRUCTIONS,
        _TransportOptions(
            registry=registry,
            runtime=runtime,
            auth=auth,
            allow_unauthenticated_local=allow_unauthenticated_local,
            manage_runtime_lifecycle=manage_runtime_lifecycle,
        ),
    )
    for spec in facade.list_tools():
        annotations = _mcp_annotations(spec)
        transport.add_tool(
            FunctionTool(
                name=spec.name,
                description=spec.description,
                parameters=spec.input_schema,
                output_schema=spec.output_schema,
                annotations=(ToolAnnotations(**annotations) if annotations is not None else None),
                fn=_tool_handler(facade, spec.name),
                return_type=dict,
                run_in_thread=False,
            )
        )
    return transport


def require_explorer_extra() -> None:
    """Fail with one message wherever the optional Explorer extra is missing.

    The reader server never needs ``prefab_ui``. The Explorer factory and the
    ``harborrag-mcp-ui`` entry point both call this, so the check exists once.

    Raises:
        RuntimeError: The ``ui`` extra is not installed.
    """
    import importlib.util

    if importlib.util.find_spec("prefab_ui") is None:
        raise RuntimeError(
            "The Explorer MCP UI server is optional and not installed; "
            "install 'harborrag-mcp-server[ui]' to use harborrag-mcp-ui"
        )


def create_explorer_server(
    *,
    registry: McpServer | None = None,
    runtime: ReaderApplication | None = None,
    auth: AuthProvider | None = None,
    allow_unauthenticated_local: bool = False,
    manage_runtime_lifecycle: bool = False,
) -> object:
    """Create the separate HarborRAG Explorer MCP UI server.

    It serves only the Explorer MCP App, so the reader server's catalog stays
    the reader tools. The app's backends call the same audited handlers the
    reader server registers, over the same registry, configuration and audit
    trail, so the UI can reach nothing a reader tool call could not.

    Args:
        registry: Registry to serve; a default one is built over ``runtime``.
        runtime: Reader runtime whose invoker backs a default registry.
        auth: Authentication provider; required unless local stdio is allowed.
        allow_unauthenticated_local: Allow no ``auth``, for local stdio only.
        manage_runtime_lifecycle: Start and close ``runtime`` with the server.

    Returns:
        The FastMCP server, ready to run on a transport.

    Raises:
        RuntimeError: The ``ui`` extra is missing, ``auth`` is missing, or the
            configuration disables every tool the Explorer uses.
    """
    require_explorer_extra()
    from harborrag_mcp_server.server.explorer import EXPLORER_INSTRUCTIONS, build_explorer

    transport, facade = _transport(
        "HarborRAG Explorer",
        EXPLORER_INSTRUCTIONS,
        _TransportOptions(
            registry=registry,
            runtime=runtime,
            auth=auth,
            allow_unauthenticated_local=allow_unauthenticated_local,
            manage_runtime_lifecycle=manage_runtime_lifecycle,
        ),
    )
    app = build_explorer(facade, lambda name: _tool_handler(facade, name))
    if app is None:
        raise RuntimeError("every reader tool the Explorer uses is disabled in the configuration")
    transport.add_provider(app)
    return transport


def _transport(
    name: str, instructions: str, options: _TransportOptions
) -> tuple[FastMCP[None], McpServer]:
    """Build an authenticated FastMCP server and the registry facade it serves.

    The server masks error details. Otherwise FastMCP would send any exception
    escaping a tool as "Error calling tool 'x': {exc}", such as a driver error
    carrying a connection URL. The HTTP route masks to the exception type too.
    A ToolError raised by the handler is still delivered in full, so a model
    keeps the messages it corrects its next call from.
    """
    if options.auth is None and not options.allow_unauthenticated_local:
        raise RuntimeError(
            "MCP transport requires authentication; "
            "set allow_unauthenticated_local=True only for local stdio"
        )
    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "FastMCP transport is not installed; install harborrag-mcp-server"
        ) from exc

    runtime = options.runtime
    facade = options.registry or McpServer(invoker=runtime.invoker if runtime is not None else None)
    lifespan: _Lifespan | None = None
    if options.manage_runtime_lifecycle:
        if runtime is None:
            raise ValueError("runtime lifecycle management requires a runtime")
        lifespan = _runtime_lifespan(runtime)
    transport: FastMCP[None] = FastMCP(
        name,
        auth=options.auth,
        lifespan=lifespan,
        instructions=instructions,
        mask_error_details=True,
    )
    return transport, facade


def _mcp_annotations(spec: ToolSpec) -> dict[str, Any] | None:
    """Return the spec's own MCP annotations, or derive them from its behaviour."""
    if spec.annotations is not None:
        return spec.annotations
    behavior = spec.behavior
    if behavior is None:
        return None
    return {
        "readOnlyHint": behavior.read_only,
        "destructiveHint": behavior.destructive,
        "idempotentHint": behavior.idempotent,
        "openWorldHint": behavior.open_world,
    }


def _runtime_lifespan(runtime: ReaderApplication) -> _Lifespan:
    """Start the reader runtime with the server and close it on shutdown.

    Ctrl-C cancels the server task while aiobotocore is still closing its
    session, so shutdown lets the close finish before cancelling. aiobotocore
    releases its transport one event-loop turn after ``aclose()``, so the
    lifespan waits a moment before it ends.
    """

    @asynccontextmanager
    async def lifespan(server: object) -> AsyncIterator[None]:
        del server
        try:
            await runtime.start()
            yield
        finally:
            close_task = asyncio.create_task(runtime.aclose())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                await close_task
                raise
            await asyncio.sleep(_TRANSPORT_RELEASE_DELAY_S)

    return lifespan


def _tool_handler(server: McpServer, tool_name: str) -> ToolHandler:
    """Return the audited FastMCP handler that runs one registry tool.

    A tool whose schema declares no ``tenant_id``, such as ``describe_graph``,
    reads no tenant's data. Its handler authorizes the principal and leaves the
    tenant unbound, so it also works on stdio and under a wildcard grant.
    """
    tenant_scoped = _declares_tenant(server, tool_name)

    async def invoke(**arguments: object) -> dict[str, object]:
        try:
            context = (
                _request_context(
                    arguments.get("tenant_id"), server.corpus_mode, server.shared_tenant_id
                )
                if tenant_scoped
                else None
            )
            principal = (
                context.access.principal_id if context is not None else _request_principal_id()
            )
        except PermissionError as exc:
            _audit_refusal(server, tool_name, arguments, exc)
            raise _caller_error(exc) from exc
        try:
            result = await server.call_tool(
                tool_name, arguments, principal_id=principal, context=context
            )
        except HarborAuthorizationUnavailableError as exc:
            from fastmcp.exceptions import ToolError

            raise ToolError(_AUTHORIZATION_UNAVAILABLE) from exc
        except (ValueError, TypeError, PermissionError) as exc:
            if is_caller_correctable(exc):
                raise _caller_error(exc) from exc
            raise
        if tool_reported_error(result):
            from fastmcp.exceptions import ToolError

            raise ToolError(_reported_failure_message(result))
        return result

    invoke.__name__ = tool_name
    return invoke


def _audit_refusal(
    server: McpServer, tool_name: str, arguments: dict[str, object], error: PermissionError
) -> None:
    """Record a call refused before it reached ``call_tool``.

    A token probing another tenant or holding the wrong role is refused here,
    and the audit trail exists to show exactly that event.
    """
    subject = _token_subject()
    invocation_id = server.audit.start(tool_name, arguments, principal_id=subject)
    server.audit.finish(
        invocation_id,
        tool_name,
        principal_id=subject,
        outcome="error",
        error_type=type(error).__name__,
    )


def _reported_failure_message(result: dict[str, object]) -> str:
    """Return a tool's own failure message, led by its ``error_code`` when it has one.

    A ToolError reaches an MCP client as text only, so the code that tells the
    client whose problem the failure is travels at the start of the message.
    """
    message = result.get("error")
    text = message if isinstance(message, str) else _REPORTED_FAILURE_FALLBACK
    code = result.get("error_code")
    return f"{code}: {text}" if isinstance(code, str) and code else text


def _caller_error(exc: Exception) -> Exception:
    """Deliver a caller-correctable refusal in full through the masked transport.

    ``mask_error_details`` reduces any other exception to "Error calling tool
    'x'". Schema and argument-budget violations, an unknown or disabled tool and
    a tenant refusal are the messages a model corrects its next call from. Only
    refusals raised before the tool runs qualify, from the request-context check
    here and those ``call_tool`` marks, so an error from the tool's own
    execution stays masked whatever its type. The audit trail has already
    recorded the original type.
    """
    from fastmcp.exceptions import ToolError

    return ToolError(str(exc))


def _declares_tenant(server: McpServer, tool_name: str) -> bool:
    """Return whether ``tool_name`` binds to a tenant, read from its input schema.

    Fail closed: a tool whose spec cannot be resolved is treated as scoped, so
    a lookup miss can only ever refuse a call, never widen one.
    """
    spec = next((item for item in server.list_tools() if item.name == tool_name), None)
    if spec is None:
        return True
    properties = spec.input_schema.get("properties")
    return not isinstance(properties, dict) or "tenant_id" in properties


def _token_subject() -> str:
    """Name the caller for the audit record of a refused request.

    This runs because the request was refused, so it authorizes nothing. An
    audit record naming the token beats one naming nobody.
    """
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        return _LOCAL_PRINCIPAL
    return _principal_of(token)


def _request_principal_id(tenant_id: object | None = None) -> str:
    """Authorize the request's token for tool calls and return its principal.

    Args:
        tenant_id: Tenant the call names; checked against the token's grants
            when it is a string.

    Returns:
        The token's principal, or the local principal when there is no token.

    Raises:
        PermissionError: The token is neither a reader nor an owner, or it does
            not grant ``tenant_id``.
    """
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        return _LOCAL_PRINCIPAL
    claims = token.claims or {}
    if claims.get("role") not in _TOOL_ROLES:
        raise PermissionError("MCP tools require a reader or owner token")
    if isinstance(tenant_id, str):
        authorize_claimed_tenant(claims, tenant_id)
    return _principal_of(token)


def _principal_of(token: AccessToken) -> str:
    """Return the token's subject claim, else its client ID."""
    subject = (token.claims or {}).get("sub")
    if isinstance(subject, str) and subject.strip():
        return subject
    if token.client_id:
        return token.client_id
    return _UNKNOWN_PRINCIPAL


def _request_context(
    tenant_id: object | None,
    corpus_mode: Literal["source_acl", "tenant_shared"],
    shared_tenant_id: str | None,
) -> ToolInvocationContext:
    """Bind the call to one tenant and the caller's principal.

    A token granting exactly one tenant binds that tenant. Otherwise the call
    must name one. Only the shared tenant uses the server's corpus mode.

    Args:
        tenant_id: Tenant the call names, if any.
        corpus_mode: The server's corpus access mode.
        shared_tenant_id: Tenant whose corpus is shared, if the server has one.

    Returns:
        The invocation context for the bound tenant.

    Raises:
        PermissionError: The token or the call does not settle one tenant.
    """
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    principal = _request_principal_id(tenant_id)
    if token is None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise PermissionError("local tool calls require an explicit tenant")
        bound_tenant = tenant_id.strip()
    else:
        from harborrag_mcp_server.server.http_auth import allowed_tenants

        grants = allowed_tenants(token.claims or {})
        if len(grants) == 1 and "*" not in grants:
            bound_tenant = next(iter(grants))
        elif isinstance(tenant_id, str) and tenant_id.strip():
            bound_tenant = tenant_id.strip()
        else:
            raise PermissionError("token must bind one tenant or the call must specify a tenant")
    return ToolInvocationContext(
        access=AccessContext(
            principal_id=principal,
            tenant_id=TenantId(bound_tenant),
            corpus_mode=corpus_mode if bound_tenant == shared_tenant_id else "source_acl",
        )
    )


__all__ = [
    "BaseMcpServer",
    "McpServer",
    "call_tool",
    "create_explorer_server",
    "create_mcp_server",
    "require_explorer_extra",
    "list_tools",
]
