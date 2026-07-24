from __future__ import annotations

from typing import Any

from harborrag_mcp.server.base import BaseMcpServer
from harborrag_mcp.server.server import McpServer


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


__all__ = ["BaseMcpServer", "McpServer", "call_tool", "list_tools"]
