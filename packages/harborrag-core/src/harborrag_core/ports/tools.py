"""Transport-independent tool invocation and audit ports."""

from __future__ import annotations

from typing import Protocol

from harborrag_core.contracts.tools import BaseTool as BaseTool
from harborrag_core.contracts.tools import ToolInvocationContext, ToolSpec


class ToolInvokerPort(Protocol):
    def list_tools(self) -> list[ToolSpec]: ...

    async def invoke(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        context: ToolInvocationContext,
    ) -> dict[str, object]: ...


class ToolExecutionAudit(Protocol):
    async def record(self, context: ToolInvocationContext, name: str, outcome: str) -> None: ...


class ToolAccessPolicy(Protocol):
    def allowed(self, context: ToolInvocationContext, name: str) -> bool: ...
