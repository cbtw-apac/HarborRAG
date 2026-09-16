"""Runtime adapter exposing retrieval façades as bounded agent read tools."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from harborrag_engine.agent import AgentToolSpec
from harborrag_runtime.agent.memory_tool_specs import (
    MANAGE_MEMORY_TOOL,
    MEMORY_AGENT_TOOL_SPECS,
    SEARCH_MEMORY_TOOL,
)
from harborrag_runtime.agent.memory_tools import AgentMemoryTools
from harborrag_runtime.tools.base import BaseTool, ToolSpec
from harborrag_runtime.tools.budgets import ToolBudget, result_count
from harborrag_runtime.tools.catalog_factory import build_reader_tool_catalog
from harborrag_runtime.tools.references import KnowledgeReferenceStore

if TYPE_CHECKING:
    from harborrag_core.ports.memory import MemoryIndex, MemoryOwner, MemoryRepository
    from harborrag_runtime.sdk import HarborRAG

logger = logging.getLogger("harborrag.runtime.agent.tools")


@dataclass(slots=True)
class RuntimeAgentToolProvider:
    """Translate engine tool calls into the shared runtime SDK façades.

    ``memories``/``index``/``memory_owner`` are the conversation-memory
    collaborators the application owns. They arrive per run, already bound to
    the authenticated caller, which is what makes the memory tools' owner
    server-side rather than model-supplied. Absent -- or with
    ``memory_tools_enabled`` off, which is the default -- the memory tools are
    neither advertised nor callable.
    """

    runtime: HarborRAG
    memories: MemoryRepository | None = None
    index: MemoryIndex | None = None
    memory_owner: MemoryOwner | None = None
    memory_tools_enabled: bool = False
    # Opaque cursors and node handles are issued from here, so the store has to
    # outlive one run: a ``cur_*`` handle the model saw last turn is replayed
    # from conversation history this turn, and a per-provider store would have
    # forgotten it, making cross-turn pagination impossible. Sharing one is safe
    # because ``resolve`` re-binds every read to its own tenant and principal.
    # The MCP transport holds one for the whole server for the same reason.
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)
    # ``McpToolPolicy`` *is* this budget with an MCP label, so the two
    # transports cannot drift: MCP narrows these per tenant through its
    # configuration layer, the agent loop runs at the shared ceiling.
    budget: ToolBudget = field(
        default_factory=lambda: ToolBudget(label="Agent", detail_in_errors=True)
    )
    _tools: dict[str, BaseTool] = field(init=False, repr=False)
    _memory_specs: dict[str, ToolSpec] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tools = {
            tool.spec.name: tool
            for tool in build_reader_tool_catalog(self.runtime, self.references)
        }
        self._memory_specs = {spec.name: spec for spec in MEMORY_AGENT_TOOL_SPECS}

    def list_tools(self, tenant_id: str | None = None) -> list[AgentToolSpec]:
        del tenant_id
        specs = [cast("AgentToolSpec", tool.spec) for tool in self._tools.values()]
        if self._memory_available():
            # Advertised only when a bound owner and a store exist, so a tool
            # that could only ever error is never offered to the model. The
            # engine's read-only filter then drops ``manage_memory`` on its
            # own, since that spec declares the ``write`` capability.
            specs.extend(cast("AgentToolSpec", spec) for spec in MEMORY_AGENT_TOOL_SPECS)
        return specs

    def _memory_available(self) -> bool:
        """Whether the memory tools are switched on *and* have what they need."""

        return (
            self.memory_tools_enabled
            and self.memories is not None
            and self.memory_owner is not None
        )

    async def _memory_tools(self) -> AgentMemoryTools | None:
        """Bind the memory tools to this run's owner, or ``None`` when unavailable."""

        if not self._memory_available() or self.memories is None or self.memory_owner is None:
            return None
        service = self.runtime._memory_context_service()
        return AgentMemoryTools(
            owner=self.memory_owner,
            memories=self.memories,
            policy=service.policy,
            index=self.index,
            embedder=await service.embedder(),
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]:
        values = dict(arguments or {})
        try:
            spec = self._spec_for(name)
            if spec is None:
                return {"ok": False, "error": "agent tool is not available"}
            # The same ceilings the MCP transport applies to this same catalog,
            # and applied to every tool it dispatches rather than to the
            # retrieval ones only: the memory schemas carry the length and
            # additionalProperties bounds the model was shown, so skipping them
            # for memory would advertise a contract nothing enforced.
            #
            # Each tool's own hand-written guards bound individual arguments;
            # only this validates the call against the schema the model was
            # shown. Output *schema* validation stays with MCP: the memory tools
            # declare none.
            self.budget.check_call(spec, values)
            result = await self._dispatch(name, values, principal_id=principal_id)
            if result is None:
                return {"ok": False, "error": "agent tool is not available"}
            self.budget.check_results(result_count(result))
            # The engine truncates every result to MAX_TOOL_RESULT_CHARS before
            # it reaches model context, so this is not the context bound; it is
            # the ceiling on what one tool may hand back at all, which keeps a
            # runaway payload from being serialized and truncated needlessly.
            self.budget.check_output(result)
            return result
        except PermissionError as exc:
            # A disabled capability is a contract the model can read and work
            # around, so it reaches the loop as an ordinary tool error rather
            # than the opaque generic failure below.
            return {"ok": False, "error": str(exc)}
        except (TypeError, ValueError) as exc:
            # A budget or schema rejection reaches the model as an ordinary tool
            # error, so the loop can narrow its request and continue.
            return {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("agent retrieval tool %r raised during call_tool", name)
            return {"ok": False, "error": "agent retrieval tool failed"}

    def _spec_for(self, name: str) -> ToolSpec | None:
        """The spec ``name`` dispatches to, or ``None`` when it is unavailable.

        A memory tool named while the feature is off resolves to nothing, so it
        answers exactly like an unknown tool and switching the flag off cannot
        be detected as a different kind of failure.
        """

        tool = self._tools.get(name)
        if tool is not None:
            return tool.spec
        if self._memory_available():
            return self._memory_specs.get(name)
        return None

    async def _dispatch(
        self,
        name: str,
        values: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object] | None:
        """Run the validated call, or ``None`` when the tool went away."""

        memory = await self._memory_operation(name, values)
        if memory is not None:
            return memory
        tool = self._tools.get(name)
        if tool is None:
            return None
        return await tool.call(values, principal_id=principal_id)

    async def _memory_operation(
        self,
        name: str,
        values: dict[str, object],
    ) -> dict[str, object] | None:
        """Run one memory tool, or ``None`` when it is not one or is unavailable.

        Availability is already decided by ``_spec_for``; returning ``None``
        here lets the caller answer exactly like an unknown tool.
        """

        if name not in {SEARCH_MEMORY_TOOL, MANAGE_MEMORY_TOOL}:
            return None
        tools = await self._memory_tools()
        if tools is None:
            return None
        if name == SEARCH_MEMORY_TOOL:
            return await tools.search(values)
        return await tools.manage(values)


__all__ = ["RuntimeAgentToolProvider"]
