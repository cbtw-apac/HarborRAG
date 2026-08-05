"""Tests for the bounded agent orchestration engine."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
    HarborToolCall,
    HarborToolCallFunction,
)
from harborrag_engine.agent import AgentRunOptions, AgentService


@dataclass(frozen=True)
class _Spec:
    name: str
    description: str = "test tool"
    input_schema: dict = None  # type: ignore[assignment]
    capability: str = "read"

    def __post_init__(self) -> None:
        if self.input_schema is None:
            object.__setattr__(
                self,
                "input_schema",
                {
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string"},
                        "observe_graph": {"type": "boolean"},
                    },
                },
            )


class _Tools:
    def __init__(self) -> None:
        self.specs = [
            _Spec("vector_search_advanced"),
            _Spec("graph_path_search"),
            _Spec("agent"),
        ]
        self.calls: list[tuple[str, dict[str, object], str]] = []

    def list_tools(self, tenant_id=None):
        self.listed_for = tenant_id
        return self.specs

    async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
        payload = dict(arguments or {})
        self.calls.append((name, payload, principal_id))
        return {"ok": True, "results": [{"text": f"result from {name}"}]}


class _Memory:
    def __init__(self) -> None:
        self.turns = {}

    async def recent(self, identity, *, limit=2):
        return self.turns.get(identity, ())[-limit:]

    async def append(self, identity, turn):
        self.turns[identity] = (*self.turns.get(identity, ()), turn)

    async def clear(self, identity):
        self.turns.pop(identity, None)


class _Chat:
    def __init__(self, responses: list[HarborChatResponse]) -> None:
        self.responses = responses
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def _response(*, call: tuple[str, str, str] | None = None, text: str = "answer"):
    tool_calls = ()
    content = text
    finish_reason = "stop"
    if call is not None:
        call_id, name, arguments = call
        tool_calls = (
            HarborToolCall(
                id=call_id,
                function=HarborToolCallFunction(
                    name=name,
                    arguments=arguments,
                    parsed_arguments=json.loads(arguments),
                ),
            ),
        )
        content = None
        finish_reason = "tool_calls"
    return HarborChatResponse(
        id="response",
        logical_model="primary",
        provider="mock",
        provider_model="mock-chat",
        deployment="private",
        message=HarborChatMessage.assistant(content, tool_calls=tool_calls),
        finish_reason=finish_reason,
        usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


@pytest.mark.asyncio
async def test_agent_runs_multiple_tool_hops_and_enforces_identity() -> None:
    tools = _Tools()
    chat = _Chat(
        [
            _response(call=("call-1", "vector_search_advanced", '{"tenant_id":"OTHER"}')),
            _response(call=("call-2", "graph_path_search", '{"tenant_id":"OTHER"}')),
            _response(text="grounded answer"),
        ]
    )
    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("multi-hop question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            graph_search=True,
        ),
    )

    assert result.response.text == "grounded answer"
    assert result.turns == 3
    assert result.usage.total_tokens == 6
    assert [execution.tool for execution in result.executions] == [
        "vector_search_advanced",
        "graph_path_search",
    ]
    assert [call[1]["tenant_id"] for call in tools.calls] == ["ACME", "ACME"]
    assert [call[2] for call in tools.calls] == ["reader-1", "reader-1"]
    assert chat.requests[1].messages[-1].role.value == "tool"


@pytest.mark.asyncio
async def test_agent_graph_switch_filters_graph_capabilities() -> None:
    tools = _Tools()
    chat = _Chat([_response(text="answer")])

    await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
        ),
    )

    definitions = chat.requests[0].tools
    assert [tool.function.name for tool in definitions] == ["vector_search_advanced"]
    assert "observe_graph" not in definitions[0].function.parameters["properties"]


@pytest.mark.asyncio
async def test_agent_forces_final_synthesis_when_step_budget_is_used() -> None:
    tools = _Tools()
    chat = _Chat(
        [
            _response(call=("call-1", "vector_search_advanced", "{}")),
            _response(text="budgeted answer"),
        ]
    )

    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            max_steps=1,
        ),
    )

    assert result.response.text == "budgeted answer"
    assert result.turns == 2
    assert chat.requests[1].tools == ()
    assert "budget is exhausted" in chat.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_agent_recalls_only_two_latest_turns_and_sets_user_metadata() -> None:
    tools = _Tools()
    chat = _Chat(
        [
            _response(text="first answer"),
            _response(text="second answer"),
            _response(text="third answer"),
            _response(text="fourth answer"),
        ]
    )
    service = AgentService(chat, tools, memory=_Memory())

    await service.run(
        [HarborChatMessage.user("first question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="principal-1",
            session_id="session-1",
        ),
    )
    await service.run(
        [HarborChatMessage.user("second question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="principal-1",
            session_id="session-1",
        ),
    )
    await service.run(
        [HarborChatMessage.user("third question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="principal-1",
            session_id="session-1",
        ),
    )
    await service.run(
        [HarborChatMessage.user("fourth question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="principal-1",
            session_id="session-1",
        ),
    )

    fourth = chat.requests[3]
    assert [message.content for message in fourth.messages[1:]] == [
        "second question",
        "second answer",
        "third question",
        "third answer",
        "fourth question",
    ]
    assert fourth.metadata.user_id is None
    assert fourth.metadata.conversation_id == "session-1"
