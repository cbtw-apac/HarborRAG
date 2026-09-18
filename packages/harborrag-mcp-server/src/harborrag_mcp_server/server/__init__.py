from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from harborrag_core.contracts.errors import HarborAuthorizationUnavailableError
from harborrag_core.contracts.tools import ToolInvocationContext, ToolSpec
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server.base import BaseMcpServer, tool_reported_error
from harborrag_mcp_server.server.http_auth import authorize_claimed_tenant
from harborrag_mcp_server.server.server import McpServer

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthProvider

    from harborrag_runtime.composition.readers import ReaderApplication


_SERVER_INSTRUCTIONS = (
    "Use list_sources when corpus scope is unclear, then vector_search for natural-language "
    "discovery. Use list_documents and get_document_metadata for document inventory. "
    "Set include_content=false for compact search hits; use composed_evidence_search for "
    "bounded semantic expansion followed by canonical evidence reads. Re-fetch citations "
    "with fetch_evidence and check final references with verify_citations; use get_document_context for an "
    "ordered, version-bound reading window. Call resolve_graph_nodes before graph traversal "
    "when a provider ID or title can be ambiguous. Use graph_triplet_search for exact "
    "relations, graph_subgraph_search for a neighborhood, and graph_path_search between two "
    "stable node keys. Cite immutable evidence only; graph records are navigation. Preserve "
    "stored direction and report bounded completion reasons."
)


def list_tools() -> list[dict[str, object]]:
    return [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema}
        for s in McpServer().list_tools()
    ]


async def call_tool(
    name: str, arguments: dict[str, object] | None = None, **kwargs: Any
) -> dict[str, object]:
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
    """Create a real FastMCP transport around the bounded in-process registry."""

    if auth is None and not allow_unauthenticated_local:
        raise RuntimeError(
            "MCP transport requires authentication; "
            "set allow_unauthenticated_local=True only for local stdio"
        )
    try:
        from fastmcp import FastMCP
        from fastmcp.tools import FunctionTool
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError(
            "FastMCP transport is not installed; install harborrag-mcp-server"
        ) from exc

    audit_path = Path(os.environ.get("HARBORRAG_MCP_AUDIT_PATH", ".harborrag/mcp-audit.jsonl"))
    facade = registry or McpServer(
        invoker=runtime.invoker if runtime is not None else None,
        audit=McpAuditLog(path=audit_path),
    )
    lifespan = None
    if manage_runtime_lifecycle:
        if runtime is None:
            raise ValueError("runtime lifecycle management requires a runtime")
        lifespan = _runtime_lifespan(runtime, facade)
    transport = FastMCP(
        "HarborRAG",
        auth=auth,
        lifespan=lifespan,
        instructions=_SERVER_INSTRUCTIONS,
        # FastMCP defaults this off, which sends any exception escaping a tool
        # to the client as "Error calling tool 'x': {exc}" -- a driver error
        # carrying a connection URL, or an audit error carrying a server path.
        # The HTTP route already masks to the exception type; this makes the MCP
        # transport agree. A ToolError we raise ourselves is still delivered in
        # full, so a model keeps the messages it has to self-correct from.
        mask_error_details=True,
    )

    # Registered once, from the global view. FastMCP builds its tool table at
    # startup, so tools/list cannot vary per tenant: a tool a tenant has
    # disabled is still advertised and then refused at call time with a
    # PermissionError naming it. Global enabled: false is honoured here.
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


def _mcp_annotations(spec: ToolSpec) -> dict[str, Any] | None:
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


def _runtime_lifespan(
    runtime: ReaderApplication,
    registry: McpServer,
) -> Any:
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
                # Ctrl-C cancels the server task while aiobotocore is still
                # closing its session; let the owner finish before exiting.
                await close_task
                raise
            # aiobotocore closes its aiohttp session during aclose(), while the
            # underlying transport releases on a later event-loop turn.
            await asyncio.sleep(0.25)

    return lifespan


def _tool_handler(
    server: McpServer,
    tool_name: str,
) -> Any:
    async def invoke(**arguments: object) -> dict[str, object]:
        try:
            context = _request_context(
                arguments.get("tenant_id"), server.corpus_mode, server.shared_tenant_id
            )
        except PermissionError as exc:
            # Resolving the principal as a call argument put it *before*
            # ``call_tool``, so a token probing another tenant or holding the
            # wrong role left no audit record at all -- exactly the event the
            # trail exists to show. Record the attempt and its refusal here.
            subject = _token_subject()
            invocation_id = server.audit.start(tool_name, arguments, principal_id=subject)
            server.audit.finish(
                invocation_id,
                tool_name,
                principal_id=subject,
                outcome="error",
                error_type=type(exc).__name__,
            )
            raise
        try:
            result = await server.call_tool(tool_name, arguments, context=context)
        except HarborAuthorizationUnavailableError as exc:
            from fastmcp.exceptions import ToolError

            raise ToolError(
                "AUTHORIZATION_UNAVAILABLE: The server could not verify data access. Retry later."
            ) from exc
        if tool_reported_error(result):
            from fastmcp.exceptions import ToolError

            message = result.get("error")
            raise ToolError(message if isinstance(message, str) else "tool execution failed")
        return result

    invoke.__name__ = tool_name
    return invoke


def _token_subject() -> str:
    """Best-effort caller identity for auditing a refused request.

    Deliberately performs no authorization of its own: this runs *because* the
    request was refused, and an audit line naming the token beats one naming
    nobody.
    """

    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        return "local-unauthenticated"
    claims = token.claims or {}
    subject = claims.get("sub")
    if isinstance(subject, str) and subject.strip():
        return subject
    if token.client_id:
        return token.client_id
    return "authenticated-unknown"


def _request_principal_id(tenant_id: object | None = None) -> str:
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        return "local-unauthenticated"
    claims = token.claims or {}
    if claims.get("role") not in {"reader", "owner"}:
        raise PermissionError("MCP tools require a reader or owner token")
    if isinstance(tenant_id, str):
        authorize_claimed_tenant(claims, tenant_id)
    subject = claims.get("sub")
    if isinstance(subject, str) and subject.strip():
        return subject
    if token.client_id:
        return token.client_id
    return "authenticated-unknown"


def _request_context(
    tenant_id: object | None,
    corpus_mode: Literal["source_acl", "tenant_shared"],
    shared_tenant_id: str | None,
) -> ToolInvocationContext:
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
    "create_mcp_server",
    "list_tools",
]
