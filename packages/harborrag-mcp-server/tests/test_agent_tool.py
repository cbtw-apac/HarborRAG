"""Contract tests for the MCP multi-turn agent."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
    HarborToolCall,
    HarborToolCallFunction,
)
from harborrag_mcp_server.agent import AgentTool
from harborrag_mcp_server.tools.base import McpToolSpec


class _Chat:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    async def complete(self, request, *, prompt=None):
        del prompt
        self.calls += 1
        self.requests.append(request)
        if self.calls == 1:
            message = HarborChatMessage.assistant(
                tool_calls=(
                    HarborToolCall(
                        id="call-1",
                        function=HarborToolCallFunction(
                            name="vector_search",
                            arguments='{"query":"first hop","tenant_id":"OTHER"}',
                            parsed_arguments={"query": "first hop", "tenant_id": "OTHER"},
                        ),
                    ),
                )
            )
            finish_reason = "tool_calls"
        else:
            assert request.messages[-1].role.value == "tool"
            message = HarborChatMessage.assistant("agent answer")
            finish_reason = "stop"
        return HarborChatResponse(
            id=f"chat-{self.calls}",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="private",
            message=message,
            finish_reason=finish_reason,
            usage=HarborChatUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        )


@dataclass
class _Tools:
    call: tuple[str, dict[str, object], str] | None = None

    def list_tools(self, tenant_id=None):
        self.tenant_id = tenant_id
        return [
            McpToolSpec(
                "vector_search",
                "Search evidence",
                {
                    "type": "object",
                    "required": ["query", "tenant_id"],
                    "properties": {
                        "query": {"type": "string"},
                        "tenant_id": {"type": "string"},
                    },
                },
            )
        ]

    async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
        self.call = (name, dict(arguments or {}), principal_id)
        return {"ok": True, "results": [{"text": "evidence"}]}


@pytest.mark.asyncio
async def test_agent_tool_runs_tool_loop_and_projects_safe_trace() -> None:
    chat = _Chat()
    tools = _Tools()
    agent = AgentTool(runtime=SimpleNamespace(chat=chat), tool_provider=tools)

    result = await agent.call(
        {
            "message": "follow-up question",
            "tenant_id": "ACME",
            "history": [{"role": "user", "content": "earlier question"}],
            "max_steps": 3,
            "session_id": "session-1",
        },
        principal_id="mcp-user",
    )

    assert result["ok"] is True
    assert result["message"] == "agent answer"
    assert result["turns"] == 2
    assert result["tool_call_count"] == 1
    assert result["usage"]["total_tokens"] == 6
    assert result["tool_calls"] == [{"step": 1, "tool": "vector_search", "ok": True}]
    assert tools.call == (
        "vector_search",
        {"query": "first hop", "tenant_id": "ACME"},
        "mcp-user",
    )
    assert chat.requests[-1].metadata.user_id is None
    assert chat.requests[-1].metadata.conversation_id == "session-1"


@pytest.mark.asyncio
async def test_agent_tool_generates_session_when_omitted() -> None:
    chat = _Chat()
    agent = AgentTool(runtime=SimpleNamespace(chat=chat), tool_provider=_Tools())

    result = await agent.call(
        {"message": "question", "tenant_id": "ACME"},
        principal_id="mcp-user",
    )

    assert result["ok"] is True
    assert result["session_id"].startswith("session-")
    assert chat.requests[-1].metadata.conversation_id == result["session_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"message": "hello", "tenant_id": "ACME", "max_steps": 0},
        {"message": "hello", "tenant_id": "ACME", "history": "invalid"},
        {
            "message": "hello",
            "tenant_id": "ACME",
            "history": [{"role": "system", "content": "not allowed"}],
        },
    ],
)
async def test_agent_tool_rejects_invalid_direct_inputs(arguments) -> None:
    agent = AgentTool(runtime=SimpleNamespace(chat=_Chat()), tool_provider=_Tools())

    assert (await agent.call(arguments, principal_id="mcp-user"))["ok"] is False
