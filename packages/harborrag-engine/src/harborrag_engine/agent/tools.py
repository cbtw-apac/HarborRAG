"""Transport-independent checked tool provider for agent runs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from harborrag_core.contracts.tools import ToolInvocationContext, ToolSpec
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_engine.tools.budgets import ToolBudget, result_count
from harborrag_engine.tools.dispatcher import ToolInvoker

from .protocols import AgentToolSpec

logger = logging.getLogger("harborrag.engine.agent.tools")

type ExtensionCall = Callable[[str, dict[str, object]], Awaitable[dict[str, object] | None]]


@dataclass(slots=True)
class ReaderAgentToolProvider:
    """Expose one checked reader invoker and optional injected tool extensions."""

    invoker: ToolInvoker
    tenant_id: str | None = None
    corpus_mode: Literal["source_acl", "tenant_shared"] = "source_acl"
    budget: ToolBudget = field(
        default_factory=lambda: ToolBudget(label="Agent", detail_in_errors=True)
    )
    extension_specs: tuple[ToolSpec, ...] = ()
    extension_call: ExtensionCall | None = None
    _reader_names: frozenset[str] = field(init=False, repr=False)
    _extensions: dict[str, ToolSpec] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._reader_names = frozenset(spec.name for spec in self.invoker.list_tools())
        self._extensions = {spec.name: spec for spec in self.extension_specs}
        if self._reader_names & self._extensions.keys():
            raise ValueError("agent extension tool duplicates a reader tool")
        if self._extensions and self.extension_call is None:
            raise ValueError("agent extension tools require a handler")

    def list_tools(self, tenant_id: str | None = None) -> list[AgentToolSpec]:
        del tenant_id
        return [
            *(cast("AgentToolSpec", spec) for spec in self.invoker.list_tools()),
            *(cast("AgentToolSpec", spec) for spec in self.extension_specs),
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]:
        values = dict(arguments or {})
        try:
            if name in self._reader_names:
                return await self._call_reader(name, values, principal_id=principal_id)
            spec = self._extensions.get(name)
            if spec is None or self.extension_call is None:
                return {"ok": False, "error": "agent tool is not available"}
            self.budget.check_call(spec, values)
            result = await self.extension_call(name, values)
            if result is None:
                return {"ok": False, "error": "agent tool is not available"}
            self.budget.check_results(result_count(result))
            self.budget.check_output(result)
            if spec.output_schema is not None:
                self.budget.check_output_schema(result, spec.output_schema)
            return result
        except (PermissionError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("agent tool %r raised during call_tool", name)
            return {"ok": False, "error": "agent retrieval tool failed"}

    async def _call_reader(
        self, name: str, values: dict[str, object], *, principal_id: str
    ) -> dict[str, object]:
        spec = next(spec for spec in self.invoker.list_tools() if spec.name == name)
        tenant = values.get("tenant_id")
        if not isinstance(tenant, str):
            if "tenant_id" in spec.input_schema.get("properties", {}):
                raise ValueError("tenant_id is required")
            tenant = self.tenant_id or "local"
        if self.tenant_id is not None and tenant != self.tenant_id:
            raise PermissionError("reader tool tenant does not match authenticated context")
        context = ToolInvocationContext(
            AccessContext(
                principal_id=principal_id,
                tenant_id=TenantId(self.tenant_id or tenant),
                corpus_mode=self.corpus_mode,
            )
        )
        return await self.invoker.invoke(name, values, context=context, budget=self.budget)


__all__ = ["ReaderAgentToolProvider"]
