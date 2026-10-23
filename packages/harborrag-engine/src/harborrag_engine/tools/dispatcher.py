"""Checked execution shared by MCP and agent consumers."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

from harborrag_core.contracts.tools import BaseTool, ToolInvocationContext, ToolSpec
from harborrag_core.ports.tools import ToolAccessPolicy, ToolExecutionAudit

from .budgets import ToolBudget, result_count
from .context import current_invocation


@dataclass(slots=True)
class ToolInvoker:
    tools: list[BaseTool]
    budget: ToolBudget = field(default_factory=ToolBudget)
    audit: ToolExecutionAudit | None = None
    access_policy: ToolAccessPolicy | None = None
    _context_aware: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._context_aware = frozenset(
            tool.spec.name
            for tool in self.tools
            if "context" in inspect.signature(tool.call).parameters
        )

    def list_tools(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools]

    async def invoke(  # noqa: PLR0913 - caller policy and catalog overrides are independent
        self,
        name: str,
        arguments: dict[str, object],
        *,
        context: ToolInvocationContext,
        budget: ToolBudget | None = None,
        spec: ToolSpec | None = None,
        enabled: bool = True,
        defaults: dict[str, object] | None = None,
    ) -> dict[str, object]:
        token = None
        try:
            if not enabled or (
                self.access_policy is not None and not self.access_policy.allowed(context, name)
            ):
                raise PermissionError(f"tool {name} is disabled")
            tool = next((item for item in self.tools if item.spec.name == name), None)
            if tool is None:
                raise ValueError(f"Unknown tool: {name}")
            values = {**(defaults or {}), **arguments}
            active_spec = spec or tool.spec
            if "tenant_id" in active_spec.input_schema.get("properties", {}) and str(
                context.access.tenant_id
            ) != values.get("tenant_id"):
                raise PermissionError("reader tool tenant does not match authenticated context")
            selected = self.budget.narrowed(budget) if budget is not None else self.budget
            selected.check_call(active_spec, values)
            token = current_invocation.set(context)
            if name in self._context_aware:
                result = await tool.call(
                    values, principal_id=context.access.principal_id, context=context
                )
            else:
                # Custom tools that still implement the previous contract
                # remain callable during the compatibility window.
                result = await tool.call(values, principal_id=context.access.principal_id)
            selected.check_results(result_count(result))
            selected.check_output(result)
            selected.check_output_schema(result, active_spec.output_schema)
        except BaseException:
            if self.audit is not None:
                await self.audit.record(context, name, "error")
            raise
        finally:
            if token is not None:
                current_invocation.reset(token)
        if self.audit is not None:
            await self.audit.record(context, name, "success" if result.get("ok", True) else "error")
        return result
