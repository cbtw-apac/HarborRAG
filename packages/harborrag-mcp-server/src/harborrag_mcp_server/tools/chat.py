"""Provider-neutral chat completion tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatMetadata,
    HarborChatRequest,
)
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.memory import (
    ConversationIdentity,
    ConversationRepository,
    ConversationTurn,
    InMemoryConversationMemory,
    new_session_id,
)

from .base import BaseMcpTool, McpToolSpec
from .retrieval_inputs import TENANT_PROPERTY, integer, number, optional_text, text

if TYPE_CHECKING:
    from harborrag_runtime.sdk import HarborRAG

_DEFAULT_MAX_TOKENS = 1024
_MAX_TOKENS = 32_768


@dataclass(slots=True)
class ChatTool(BaseMcpTool):
    """Generate one bounded chat response through the shared runtime."""

    runtime: HarborRAG | None = None
    memory: ConversationRepository = field(default_factory=InMemoryConversationMemory)
    spec = McpToolSpec(
        "chat",
        "Generate a tenant-scoped response with a configured Harbor chat model.",
        {
            "type": "object",
            "required": ["message", "tenant_id"],
            "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 65_536},
                "tenant_id": TENANT_PROPERTY,
                "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "system": {"type": "string", "minLength": 1, "maxLength": 65_536},
                "prompt": {
                    "type": "string",
                    "enum": [prompt.value for prompt in ChatPrompt],
                    "default": ChatPrompt.DEFAULT.value,
                },
                "model": {"type": "string", "minLength": 1, "maxLength": 128},
                "temperature": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 2.0,
                    "default": 0.2,
                },
                "max_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TOKENS,
                    "default": _DEFAULT_MAX_TOKENS,
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
        if self.runtime is None:
            return {"ok": False, "error": "chat backend is not configured"}
        try:
            tenant_id = text(arguments, "tenant_id")
            session_id = optional_text(arguments, "session_id") or new_session_id()
            identity = ConversationIdentity(tenant_id, principal_id, session_id)
            if not await self.memory.exists(identity):
                await self.memory.create(identity)
            turns = await self.memory.recent(identity, limit=2)
            messages = []
            system = optional_text(arguments, "system")
            if system is not None:
                messages.append(HarborChatMessage.system(system))
            message = text(arguments, "message")
            for turn in turns:
                messages.extend(
                    (
                        HarborChatMessage.user(turn.user_content),
                        HarborChatMessage.assistant(turn.assistant_content),
                    )
                )
            messages.append(HarborChatMessage.user(message))
            prompt = ChatPrompt(
                text(
                    {"prompt": arguments.get("prompt", ChatPrompt.DEFAULT.value)},
                    "prompt",
                )
            )
            request = HarborChatRequest(
                messages=tuple(messages),
                logical_model=optional_text(arguments, "model"),
                temperature=number(
                    arguments,
                    "temperature",
                    0.2,
                    minimum=0.0,
                    maximum=2.0,
                ),
                max_tokens=integer(
                    arguments,
                    "max_tokens",
                    _DEFAULT_MAX_TOKENS,
                    minimum=1,
                    maximum=_MAX_TOKENS,
                ),
                metadata=HarborChatMetadata(
                    tenant_id=tenant_id,
                    conversation_id=session_id,
                ),
                sensitive=True,
            )
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "chat backend failed"}
        try:
            response = await self.runtime.chat.complete(request, prompt=prompt)
            await self.memory.append(identity, ConversationTurn(message, response.text))
        except Exception:
            return {"ok": False, "error": "chat backend failed"}
        return {
            "ok": True,
            "id": response.id,
            "model": response.logical_model,
            "provider": response.provider,
            "message": response.text,
            "finish_reason": str(response.finish_reason),
            "usage": response.usage.model_dump(mode="json"),
            "session_id": session_id,
        }
