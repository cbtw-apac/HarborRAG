"""Authenticated HTTP application service for bounded multi-turn agents."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest, HarborChatResponse
from harborrag_runtime.agent import AgentRunOptions, AgentService
from harborrag_runtime.agent.tools import RuntimeAgentToolProvider
from harborrag_runtime.chat import ChatFacade, ChatPrompt
from harborrag_runtime.memory import ConversationIdentity, ConversationRepository
from harborrag_runtime.sdk import HarborRAG

from .options import AgentExecutionOptions

type RuntimeProvider = Callable[[], HarborRAG]

logger = logging.getLogger("harborrag.app.workflow_control.agent")


@dataclass(frozen=True, slots=True)
class _DefaultPromptChat:
    facade: ChatFacade

    async def complete(self, request: HarborChatRequest) -> HarborChatResponse:
        return await self.facade.complete(request, prompt=ChatPrompt.DEFAULT)


class AgentApplicationService:
    """Execute the shared engine agent over runtime-native retrieval tools."""

    def __init__(
        self,
        runtime_provider: RuntimeProvider,
        *,
        memory: ConversationRepository,
    ) -> None:
        self._runtime_provider = runtime_provider
        self._memory = memory

    async def complete(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        identity = ConversationIdentity(tenant_id, principal_id, options.session_id)
        if not await self._memory.exists(identity):
            raise HarborNotFoundError("Conversation session was not found")
        try:
            runtime = self._runtime_provider()
            result = await AgentService(
                _DefaultPromptChat(runtime.chat),
                RuntimeAgentToolProvider(runtime),
                memory=self._memory,
            ).run(
                (HarborChatMessage.user(query),),
                AgentRunOptions(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    session_id=options.session_id,
                    graph_search=options.graph_search,
                    max_steps=options.max_steps,
                ),
            )
            response = result.response
            return AppResponse(
                True,
                {
                    "id": response.id,
                    "model": response.logical_model,
                    "provider": response.provider,
                    "provider_model": response.provider_model,
                    "message": {"role": "assistant", "content": response.text},
                    "finish_reason": str(response.finish_reason),
                    "usage": result.usage.model_dump(mode="json"),
                    "turns": result.turns,
                    "tool_call_count": len(result.executions),
                    "tool_calls": [
                        {
                            "step": execution.step,
                            "tool": execution.tool,
                            "ok": execution.ok,
                        }
                        for execution in result.executions
                    ],
                    "session_id": options.session_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(logger, exc, "run agent completion")


__all__ = ["AgentApplicationService"]
