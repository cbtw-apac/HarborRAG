"""Bounded multi-turn agent exposed through the audited MCP registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest, HarborChatResponse
from harborrag_runtime.agent import AgentRunOptions, AgentService
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.memory import (
    ConversationIdentity,
    ConversationRepository,
    InMemoryConversationMemory,
    new_session_id,
)

from .base import BaseMcpTool, McpToolSpec
from .retrieval_inputs import TENANT_PROPERTY, boolean, integer, optional_text, text

if TYPE_CHECKING:
    from harborrag_mcp_server.server.base import BaseMcpServer
    from harborrag_runtime.sdk import HarborRAG

_MAX_STEPS = 8
_MAX_HISTORY = 50


@dataclass(slots=True)
class AgentTool(BaseMcpTool):
    """Answer multi-hop questions by repeatedly invoking enabled read tools."""

    runtime: HarborRAG | None = None
    tool_provider: BaseMcpServer | None = None
    memory: ConversationRepository = field(default_factory=InMemoryConversationMemory)
    spec = McpToolSpec(
        "agent",
        "Run a bounded multi-turn agent that can call enabled HarborRAG tools repeatedly.",
        {
            "type": "object",
            "required": ["message", "tenant_id"],
            "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 65_536},
                "tenant_id": TENANT_PROPERTY,
                "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "history": {
                    "type": "array",
                    "maxItems": _MAX_HISTORY,
                    "default": [],
                    "items": {
                        "type": "object",
                        "required": ["role", "content"],
                        "properties": {
                            "role": {"type": "string", "enum": ["user", "assistant"]},
                            "content": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 65_536,
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "prompt": {
                    "type": "string",
                    "enum": [prompt.value for prompt in ChatPrompt],
                    "default": ChatPrompt.DEFAULT.value,
                },
                "graph_search": {"type": "boolean", "default": False},
                "max_steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_STEPS,
                    "default": 4,
                },
            },
            "additionalProperties": False,
        },
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        if self.runtime is None or self.tool_provider is None:
            return {"ok": False, "error": "agent backend is not configured"}
        try:
            tenant_id = text(arguments, "tenant_id")
            messages = [*_history(arguments), HarborChatMessage.user(text(arguments, "message"))]
            prompt_value = arguments.get("prompt", ChatPrompt.DEFAULT.value)
            if not isinstance(prompt_value, str):
                raise ValueError("prompt must be a string")
            prompt = ChatPrompt(prompt_value)
            graph_search = boolean(arguments, "graph_search", False)
            max_steps = integer(arguments, "max_steps", 4, minimum=1, maximum=_MAX_STEPS)
            session_id = optional_text(arguments, "session_id") or new_session_id()
            identity = ConversationIdentity(tenant_id, principal_id, session_id)
            if not await self.memory.exists(identity):
                await self.memory.create(identity)
        except (HarborValidationError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

        try:
            chat = _PromptedChat(self.runtime.chat, prompt)
            result = await AgentService(chat, self.tool_provider, memory=self.memory).run(
                messages,
                AgentRunOptions(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    session_id=session_id,
                    graph_search=graph_search,
                    max_steps=max_steps,
                ),
            )
        except Exception:
            return {"ok": False, "error": "agent backend failed"}
        response = result.response
        return {
            "ok": True,
            "id": response.id,
            "model": response.logical_model,
            "provider": response.provider,
            "message": response.text,
            "finish_reason": str(response.finish_reason),
            "usage": result.usage.model_dump(mode="json"),
            "turns": result.turns,
            "tool_call_count": len(result.executions),
            "session_id": session_id,
            "tool_calls": [
                {
                    "step": execution.step,
                    "tool": execution.tool,
                    "ok": execution.ok,
                }
                for execution in result.executions
            ],
        }


@dataclass(frozen=True, slots=True)
class _PromptedChat:
    """Adapt runtime-owned prompt selection to the engine's model port."""

    facade: Any
    prompt: ChatPrompt

    async def complete(self, request: HarborChatRequest) -> HarborChatResponse:
        return await self.facade.complete(request, prompt=self.prompt)


def _history(arguments: dict[str, object]) -> list[HarborChatMessage]:
    raw = arguments.get("history", [])
    if not isinstance(raw, list):
        raise TypeError("history must be an array")
    if len(raw) > _MAX_HISTORY:
        raise ValueError(f"history must contain at most {_MAX_HISTORY} messages")
    messages = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("history entries must be objects")
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
            raise ValueError("history entries require a user/assistant role and content")
        messages.append(
            HarborChatMessage.user(content)
            if role == "user"
            else HarborChatMessage.assistant(content)
        )
    return messages


__all__ = ["AgentTool"]
