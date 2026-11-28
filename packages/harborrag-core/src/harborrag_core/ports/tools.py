"""Transport-independent tool audit and access ports."""

from __future__ import annotations

from typing import Protocol

from harborrag_core.contracts.tools import BaseTool as BaseTool
from harborrag_core.contracts.tools import ToolInvocationContext


class ToolExecutionAudit(Protocol):
    async def record(self, context: ToolInvocationContext, name: str, outcome: str) -> None: ...


class ToolAccessPolicy(Protocol):
    def allowed(self, context: ToolInvocationContext, name: str) -> bool: ...
