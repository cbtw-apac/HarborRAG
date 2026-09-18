"""Bind optional runtime services to the engine's agent tool provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harborrag_engine.agent.protocols import AgentToolSpec
from harborrag_engine.agent.tools import ReaderAgentToolProvider
from harborrag_engine.tools.budgets import ToolBudget
from harborrag_engine.tools.catalog import build_reader_tool_catalog
from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_engine.tools.references import KnowledgeReferenceStore

if TYPE_CHECKING:
    from harborrag_core.ports.memory import MemoryIndex, MemoryOwner, MemoryRepository
    from harborrag_runtime.sdk import HarborRAG


@dataclass(slots=True)
class RuntimeAgentToolProvider:
    """Compose reader tools and explicitly enabled, owner-bound memory tools."""

    runtime: HarborRAG | None = None
    memories: MemoryRepository | None = None
    index: MemoryIndex | None = None
    memory_owner: MemoryOwner | None = None
    memory_tools_enabled: bool = False
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)
    reader_invoker: ToolInvoker | None = None
    tenant_id: str | None = None
    budget: ToolBudget = field(
        default_factory=lambda: ToolBudget(label="Agent", detail_in_errors=True)
    )
    _provider: ReaderAgentToolProvider = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.reader_invoker is None and self.runtime is None:
            raise ValueError("agent reader tools require an injected invoker or a runtime")
        invoker = self.reader_invoker or ToolInvoker(
            build_reader_tool_catalog(self.runtime, self.references), budget=self.budget
        )
        extensions = ()
        handler = None
        if (
            self.memory_tools_enabled
            and self.runtime is not None
            and self.memories is not None
            and self.memory_owner is not None
        ):
            from harborrag_memory.tools.memory_tool_specs import MEMORY_AGENT_TOOL_SPECS

            extensions = MEMORY_AGENT_TOOL_SPECS
            handler = self._call_memory
        self._provider = ReaderAgentToolProvider(
            invoker,
            tenant_id=self.tenant_id,
            budget=self.budget,
            extension_specs=extensions,
            extension_call=handler,
        )

    def list_tools(self, tenant_id: str | None = None) -> list[AgentToolSpec]:
        return self._provider.list_tools(tenant_id)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]:
        self._provider.budget = self.budget
        return await self._provider.call_tool(name, arguments, principal_id=principal_id)

    async def _call_memory(self, name: str, values: dict[str, object]) -> dict[str, object] | None:
        if self.runtime is None or self.memories is None or self.memory_owner is None:
            return None
        from harborrag_memory.tools.memory_tools import AgentMemoryTools

        service = self.runtime._memory_context_service()
        tools = AgentMemoryTools(
            owner=self.memory_owner,
            memories=self.memories,
            policy=service.policy,
            index=self.index,
            embedder=await service.embedder(),
        )
        return await tools.call(name, values)


__all__ = ["RuntimeAgentToolProvider"]
