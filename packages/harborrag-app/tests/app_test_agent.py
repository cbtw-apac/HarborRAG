"""Agent behavior for the shared application-service test double."""

from __future__ import annotations

from collections.abc import AsyncIterator

from harborrag_app.workflow_control import AppResponse
from harborrag_app.workflow_control.agent import AgentExecutionOptions
from harborrag_core.contracts.errors import HarborNotFoundError


class AgentServiceFixture:
    agent_calls: list[dict[str, object]]
    agent_resume_calls: list[dict[str, object]]

    def _agent_response_data(self, *, run_id: str, session_id: str) -> dict[str, object]:
        return {
            "id": "agent-1",
            "run_id": run_id,
            "model": "primary",
            "provider": "mock",
            "provider_model": "mock-chat",
            "message": {"role": "assistant", "content": "Agent response"},
            "finish_reason": "stop",
            "stop_reason": "final_answer",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            "turns": 2,
            "tool_call_count": 1,
            "tool_calls": [{"step": 1, "tool": "vector_search", "ok": True}],
            "session_id": session_id,
        }

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
            self._agent_response_data(run_id="run-1", session_id=options.session_id),
        )

    def agent_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]:
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
        return self._agent_stream_events(options.session_id)

    async def _agent_stream_events(self, session_id: str) -> AsyncIterator[dict[str, object]]:
        yield {
            "kind": "event",
            "event": {"name": "run.started", "run_id": "run-1", "data": {"step": 1}},
        }
        yield {
            "kind": "event",
            "event": {"name": "run.completed", "run_id": "run-1", "data": {"step": 1}},
        }
        yield {
            "kind": "result",
            "result": self._agent_response_data(run_id="run-1", session_id=session_id),
        }

    async def agent_resume(
        self,
        run_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        if run_id != "run-1":
            raise HarborNotFoundError("Agent run was not found")
        self.agent_resume_calls.append(
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "session_id": options.session_id,
                "graph_search": options.graph_search,
                "max_steps": options.max_steps,
            }
        )
        return AppResponse(
            True,
            self._agent_response_data(run_id=run_id, session_id=options.session_id),
        )


__all__ = ["AgentServiceFixture"]
