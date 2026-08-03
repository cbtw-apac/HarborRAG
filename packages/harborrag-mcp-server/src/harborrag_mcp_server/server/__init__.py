from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server.base import BaseMcpServer
from harborrag_mcp_server.server.server import McpServer

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthProvider


def list_tools() -> list[dict[str, object]]:
    return [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema}
        for s in McpServer().list_tools()
    ]


def call_tool(
    name: str, arguments: dict[str, object] | None = None, **kwargs: Any
) -> dict[str, object]:
    payload = dict(arguments or {})
    payload.update(kwargs)
    return McpServer().call_tool(name, payload)


def create_mcp_server(
    *,
    host: str,
    registry: McpServer | None = None,
    auth: AuthProvider | None = None,
    allow_unauthenticated_local: bool = False,
) -> object:
    """Create a real FastMCP transport around the bounded in-process registry."""

    if auth is None and not allow_unauthenticated_local:
        raise RuntimeError(
            "MCP transport requires authentication; "
            "set allow_unauthenticated_local=True only for local development"
        )
    if auth is None and allow_unauthenticated_local and not _is_loopback_host(host):
        raise RuntimeError(
            f"Refusing unauthenticated MCP server on non-loopback host: {host}. "
            "Use 127.0.0.1, localhost, or ::1."
        )
    try:
        from fastmcp import FastMCP
        from fastmcp.tools import FunctionTool
    except ImportError as exc:
        raise RuntimeError(
            "FastMCP transport is not installed; install harborrag-mcp-server[mcp]"
        ) from exc

    audit_path = Path(os.environ.get("HARBORRAG_MCP_AUDIT_PATH", ".harborrag/mcp-audit.jsonl"))
    facade = registry or McpServer(audit=McpAuditLog(path=audit_path))
    transport = FastMCP("HarborRAG", auth=auth)

    for spec in facade.list_tools():
        transport.add_tool(
            FunctionTool(
                name=spec.name,
                description=spec.description,
                parameters=spec.input_schema,
                fn=_tool_handler(facade, spec.name),
                return_type=dict,
                run_in_thread=True,
            )
        )
    return transport


def _tool_handler(
    server: McpServer,
    tool_name: str,
) -> Any:
    def invoke(**arguments: object) -> dict[str, object]:
        return server.call_tool(
            tool_name,
            arguments,
            principal_id=_request_principal_id(),
        )

    invoke.__name__ = tool_name
    return invoke


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _request_principal_id() -> str:
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        return "local-unauthenticated"
    subject = (token.claims or {}).get("sub")
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
