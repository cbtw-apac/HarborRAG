from __future__ import annotations

from abc import ABC, abstractmethod

from harborrag_runtime.tools.base import ToolSpec


class BaseMcpServer(ABC):
    """Contract for an MCP server exposing only audited service-level tools."""

    @abstractmethod
    def list_tools(self, tenant_id: str | None = None) -> list[ToolSpec]:
        raise NotImplementedError

    @abstractmethod
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]:
        raise NotImplementedError
