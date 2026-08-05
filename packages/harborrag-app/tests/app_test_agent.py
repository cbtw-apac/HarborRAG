"""Agent behavior for the shared application-service test double."""

from __future__ import annotations

from harborrag_app.workflow_control import AppResponse
from harborrag_app.workflow_control.agent import AgentExecutionOptions
from harborrag_core.contracts.errors import HarborNotFoundError


class AgentServiceFixture:
    agent_calls: list[dict[str, object]]

    async def agent_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        if (tenant_id, principal_id, options.session_id) not in self.conversation_sessions:
            raise HarborNotFoundError("Conversation session was not found")
        self.agent_calls.append(
            {
                "query": query,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "session_id": options.session_id,
                "graph_search": options.graph_search,
                "max_steps": options.max_steps,
            }
        )
        return AppResponse(
            True,
            {
                "id": "agent-1",
                "model": "primary",
                "provider": "mock",
                "provider_model": "mock-chat",
                "message": {"role": "assistant", "content": "Agent response"},
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                "turns": 2,
                "tool_call_count": 1,
                "tool_calls": [{"step": 1, "tool": "vector_search", "ok": True}],
                "session_id": options.session_id,
            },
        )


__all__ = ["AgentServiceFixture"]
