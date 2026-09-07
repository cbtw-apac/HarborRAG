from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harborrag_core.invariants import HarborInvariantError
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server.base import BaseMcpServer
from harborrag_mcp_server.server.http_auth import authorize_claimed_tenant
from harborrag_mcp_server.server.server import McpServer

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthProvider

    from harborrag_runtime.sdk import HarborRAG


_SERVER_INSTRUCTIONS = (
    "Call describe_graph when graph selectors, relations, directions, or topology are "
    "unclear. For natural-language discovery, call vector_search first and use its "
    "returned chunk_id as a graph selector. Use graph_subgraph_search for nearby "
    "context, graph_path_search when both endpoints are known, and "
    "graph_triplet_search for exact graph filters. Graph tools do not provide "
    "free-text or partial-title search. Tenant data tools require tenant_id. "
    "observe_graph=true on vector_search adds shallow provenance diagnostics; it "
    "does not retrieve additional evidence content."
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
    runtime: HarborRAG | None = None,
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
            "FastMCP transport is not installed; install harborrag-mcp-server[mcp]"
        ) from exc

    audit_path = Path(os.environ.get("HARBORRAG_MCP_AUDIT_PATH", ".harborrag/mcp-audit.jsonl"))
    facade = registry or McpServer(
        runtime=runtime,
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
    )

    for spec in facade.list_tools():
        transport.add_tool(
            FunctionTool(
                name=spec.name,
                description=spec.description,
                parameters=spec.input_schema,
                output_schema=spec.output_schema,
                annotations=(
                    ToolAnnotations(**spec.annotations) if spec.annotations is not None else None
                ),
                fn=_tool_handler(facade, spec.name),
                return_type=dict,
                run_in_thread=False,
            )
        )
    return transport


def _runtime_lifespan(
    runtime: HarborRAG,
    registry: McpServer,
) -> Any:
    @asynccontextmanager
    async def lifespan(server: object) -> AsyncIterator[None]:
        del server
        try:
            yield
        finally:
            try:
                await runtime.aclose()
            finally:
                close_memory = getattr(registry.memory, "aclose", None)
                if close_memory is not None:
                    await close_memory()

    return lifespan


def _tool_handler(
    server: McpServer,
    tool_name: str,
) -> Any:
    async def invoke(**arguments: object) -> dict[str, object]:
        return await server.call_tool(
            tool_name,
            arguments,
            principal_id=_request_principal_id(arguments.get("tenant_id")),
        )

    invoke.__name__ = tool_name
    return invoke


def _request_principal_id(tenant_id: object | None = None) -> str:
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        return "local-unauthenticated"
    claims = token.claims or {}
    if claims.get("role") != "owner":
        raise PermissionError("MCP tools require an owner token")
    if isinstance(tenant_id, str):
        authorize_claimed_tenant(claims, tenant_id)
    subject = claims.get("sub")
    if isinstance(subject, str) and subject.strip():
        return subject
    if token.client_id:
        return token.client_id
    return "authenticated-unknown"


__all__ = [
    "BaseMcpServer",
    "McpServer",
    "call_tool",
    "create_mcp_server",
    "list_tools",
]
