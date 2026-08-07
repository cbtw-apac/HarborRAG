"""Model-turn and tool-call execution primitives used by the agent loop.

Split out of ``loop.py`` so that module stays focused on step orchestration
(guard checks, checkpointing, stop-reason bookkeeping) while this one owns
the actual calls out to the chat model and the tool provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatMetadata,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatTool,
    HarborToolCall,
)
from harborrag_core.ports.agent_runs import AgentToolExecution
from harborrag_engine.conversation import ConversationIdentity, ConversationMemory, ConversationTurn

from .guard import ExecutionGuard, digest_arguments
from .protocols import AgentChatModel, AgentToolProvider, AgentToolSpec
from .schemas import AgentRunOptions
from .tool_execution import ADVANCED_VECTOR_TOOL, bounded_tool_result_content

_BLOCKED_TOOL_NAMES = frozenset({"agent", "chat"})
_GRAPH_TOOL_PREFIX = "graph_"


class ChatAndToolExecutor:
    """Bind one model turn or one batch of tool calls to their transports."""

    def __init__(
        self,
        chat: AgentChatModel,
        tools: AgentToolProvider,
        *,
        memory: ConversationMemory | None,
    ) -> None:
        self._chat = chat
        self._tools = tools
        self._memory = memory

    def available_specs(
        self,
        tenant_id: str,
        *,
        graph_search: bool,
    ) -> list[AgentToolSpec]:
        return [
            spec
            for spec in self._tools.list_tools(tenant_id)
            if spec.capability == "read"
            and spec.name not in _BLOCKED_TOOL_NAMES
            and (graph_search or not spec.name.startswith(_GRAPH_TOOL_PREFIX))
        ]

    async def complete(
        self,
        messages: Sequence[HarborChatMessage],
        options: AgentRunOptions,
        *,
        tools: tuple[HarborChatTool, ...],
        guard: ExecutionGuard | None = None,
    ) -> HarborChatResponse:
        request = HarborChatRequest(
            messages=tuple(messages),
            tools=tools,
            parallel_tool_calls=True if tools else None,
            metadata=HarborChatMetadata(
                tenant_id=options.tenant_id,
                conversation_id=options.session_id,
            ),
            sensitive=True,
        )
        coro = self._chat.complete(request)
        remaining = guard.remaining_seconds() if guard is not None else None
        if remaining is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=remaining)

    async def execute_tool_calls(
        self,
        calls: Sequence[HarborToolCall],
        *,
        step: int,
        options: AgentRunOptions,
        allowed_names: set[str],
        guard: ExecutionGuard,
    ) -> tuple[tuple[HarborChatMessage, AgentToolExecution], ...]:
        coro = asyncio.gather(
            *(
                self._execute(call, step=step, options=options, allowed_names=allowed_names)
                for call in calls
            )
        )
        remaining = guard.remaining_seconds()
        if remaining is None:
            return tuple(await coro)
        return tuple(await asyncio.wait_for(coro, timeout=remaining))

    async def remember(
        self,
        identity: ConversationIdentity | None,
        current_user_message: HarborChatMessage | None,
        response_text: str,
    ) -> None:
        if (
            self._memory is not None
            and identity is not None
            and current_user_message is not None
            and isinstance(current_user_message.content, str)
        ):
            await self._memory.append(
                identity,
                ConversationTurn(current_user_message.content, response_text),
            )

    async def _execute(
        self,
        call: HarborToolCall,
        *,
        step: int,
        options: AgentRunOptions,
        allowed_names: set[str],
    ) -> tuple[HarborChatMessage, AgentToolExecution]:
        name = call.function.name
        arguments = call.function.parsed_arguments
        digest = digest_arguments(
            arguments if isinstance(arguments, dict) else {"__unparsed__": call.function.arguments}
        )
        result = await self._invoke(name, arguments, options=options, allowed_names=allowed_names)
        content = bounded_tool_result_content(result)
        return (
            HarborChatMessage.tool(content, tool_call_id=call.id, name=name),
            AgentToolExecution(
                step=step,
                call_id=call.id,
                tool=name,
                ok=result.get("ok") is True,
                arguments_digest=digest,
            ),
        )

    async def _invoke(
        self,
        name: str,
        arguments: object,
        *,
        options: AgentRunOptions,
        allowed_names: set[str],
    ) -> dict[str, object]:
        """Run one tool call, or explain why it can't run, without ever raising."""

        if not isinstance(arguments, dict):
            return {"ok": False, "error": "invalid tool arguments"}
        if name not in allowed_names:
            return {"ok": False, "error": "tool is not available to this agent"}
        try:
            return await self._tools.call_tool(
                name,
                self._scoped_arguments(name, arguments, options),
                principal_id=options.principal_id,
            )
        except Exception:  # noqa: BLE001 - tool failures become model-visible data
            return {"ok": False, "error": "tool call failed"}

    @staticmethod
    def _scoped_arguments(
        name: str,
        arguments: dict[str, object],
        options: AgentRunOptions,
    ) -> dict[str, object]:
        """Bind a tool call to its caller's tenant, never trusting the model for it."""

        scoped = dict(arguments)
        scoped["tenant_id"] = options.tenant_id
        if name == ADVANCED_VECTOR_TOOL and not options.graph_search:
            scoped["observe_graph"] = False
        return scoped


__all__ = ["ChatAndToolExecutor"]
