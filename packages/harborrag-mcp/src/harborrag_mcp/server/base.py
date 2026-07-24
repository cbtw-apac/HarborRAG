from __future__ import annotations

from abc import ABC, abstractmethod

from harborrag_mcp.tools.base import McpToolSpec


class BaseMcpServer(ABC):
    """Contract for an MCP server exposing only audited service-level tools."""

    @abstractmethod
    def list_tools(self) -> list[McpToolSpec]:
        raise NotImplementedError

    @abstractmethod
    def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
        raise NotImplementedError
