"""Structural ports the agent loop is compiled against."""

from __future__ import annotations

from typing import Any, Protocol

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse


class AgentToolSpec(Protocol):
    """Structural subset shared with MCP tool specifications."""

    name: str
    description: str
    input_schema: dict[str, Any]
    capability: str


class AgentToolProvider(Protocol):
    """Tool transport injected into the agent engine."""

    def list_tools(self, tenant_id: str | None = None) -> list[AgentToolSpec]: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]: ...


class AgentChatModel(Protocol):
    """Minimal model port required by agent orchestration."""

    async def complete(self, request: HarborChatRequest) -> HarborChatResponse: ...


__all__ = ["AgentChatModel", "AgentToolProvider", "AgentToolSpec"]
