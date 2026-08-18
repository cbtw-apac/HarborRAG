"""Tests for the bounded agent orchestration engine's core run() behavior."""

from __future__ import annotations

import pytest
from agent_test_helpers import (
    Chat,
    Memory,
    Tools,
    many_tool_calls_response,
)
from agent_test_helpers import (
    response as _response,
)

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
    HarborToolCall,
    HarborToolCallFunction,
)
from harborrag_core.ports.agent_runs import AgentStopReason
from harborrag_engine.agent import AgentRunOptions, AgentService
from harborrag_engine.agent.tool_execution import (
    MAX_TOOL_CALLS_PER_TURN,
    MAX_TOOL_RESULT_CHARS,
    bounded_tool_result_content,
)


@pytest.mark.asyncio
async def test_agent_runs_multiple_tool_hops_and_enforces_identity() -> None:
    tools = Tools()
    chat = Chat(
        [
            _response(call=("call-1", "vector_search", '{"tenant_id":"OTHER"}')),
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
    assert result.run_id.startswith("run-")
    assert result.stop_reason is AgentStopReason.FINAL_ANSWER
    assert [execution.tool for execution in result.executions] == [
        "vector_search",
        "graph_path_search",
    ]
    assert [call[1]["tenant_id"] for call in tools.calls] == ["ACME", "ACME"]
    assert [call[2] for call in tools.calls] == ["reader-1", "reader-1"]
    assert chat.requests[1].messages[-1].role.value == "tool"


@pytest.mark.asyncio
async def test_agent_graph_switch_filters_graph_capabilities() -> None:
    tools = Tools()
    chat = Chat([_response(text="answer")])

    await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
        ),
    )

    definitions = chat.requests[0].tools
    assert [tool.function.name for tool in definitions] == ["vector_search"]
    assert "observe_graph" not in definitions[0].function.parameters["properties"]


@pytest.mark.asyncio
async def test_agent_forces_final_synthesis_when_step_budget_is_used() -> None:
    tools = Tools()
    chat = Chat(
        [
            _response(call=("call-1", "vector_search", "{}")),
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
    assert result.stop_reason is AgentStopReason.MAX_STEPS
    assert chat.requests[1].tools == ()
    assert "budget is exhausted" in chat.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_agent_recalls_only_two_latest_turns_and_sets_user_metadata() -> None:
    tools = Tools()
    chat = Chat(
        [
            _response(text="first answer"),
            _response(text="second answer"),
            _response(text="third answer"),
            _response(text="fourth answer"),
        ]
    )
    service = AgentService(chat, tools, memory=Memory())

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


@pytest.mark.asyncio
async def test_agent_caps_concurrent_tool_calls_but_replies_to_every_call() -> None:
    # A buggy provider adapter or a model prompted by untrusted retrieved
    # content could return far more tool calls than the loop intends to run
    # concurrently; every issued call_id still needs a tool-role reply or the
    # next request to most providers is malformed.
    requested = MAX_TOOL_CALLS_PER_TURN + 3
    tools = Tools()
    chat = Chat([many_tool_calls_response(requested), _response(text="answer")])

    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1"),
    )

    assert result.response.text == "answer"
    assert len(tools.calls) == MAX_TOOL_CALLS_PER_TURN
    assert len(result.executions) == requested
    assert [execution.ok for execution in result.executions[MAX_TOOL_CALLS_PER_TURN:]] == [
        False
    ] * 3

    second_request = chat.requests[1]
    tool_messages = [m for m in second_request.messages if m.role.value == "tool"]
    assert len(tool_messages) == requested
    assert {m.tool_call_id for m in tool_messages} == {
        f"call-{index}" for index in range(requested)
    }
    overflow_content = next(
        m.content for m in tool_messages if m.tool_call_id == f"call-{requested - 1}"
    )
    assert "budget exceeded" in overflow_content


@pytest.mark.asyncio
async def test_agent_rejects_non_dict_tool_arguments_without_calling_the_tool() -> None:
    tools = Tools()
    call = HarborToolCall(
        id="call-1",
        function=HarborToolCallFunction(
            name="vector_search",
            arguments="[]",
            parsed_arguments=None,
        ),
    )
    response = HarborChatResponse(
        id="response",
        logical_model="primary",
        provider="mock",
        provider_model="mock-chat",
        deployment="private",
        message=HarborChatMessage.assistant(None, tool_calls=(call,)),
        finish_reason="tool_calls",
        usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    chat = Chat([response, _response(text="answer")])

    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1"),
    )

    assert tools.calls == []
    assert result.executions[0].ok is False
    tool_message = next(
        m
        for m in chat.requests[1].messages
        if m.role.value == "tool" and m.tool_call_id == "call-1"
    )
    assert "invalid tool arguments" in tool_message.content


@pytest.mark.asyncio
async def test_agent_truncates_oversized_tool_results() -> None:
    class _HugeResultTools(Tools):
        async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
            return {"ok": True, "text": "x" * (MAX_TOOL_RESULT_CHARS * 2)}

    tools = _HugeResultTools()
    chat = Chat(
        [
            _response(call=("call-1", "vector_search", "{}")),
            _response(text="answer"),
        ]
    )

    await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1"),
    )

    tool_message = next(m for m in chat.requests[1].messages if m.role.value == "tool")
    assert len(tool_message.content) < MAX_TOOL_RESULT_CHARS * 2
    assert "truncated" in tool_message.content


@pytest.mark.asyncio
async def test_agent_bounds_circular_tool_results() -> None:
    class _CircularResultTools(Tools):
        async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
            del name, arguments, principal_id
            result: dict[str, object] = {"ok": True}
            result["self"] = result
            return result

    chat = Chat(
        [
            _response(call=("call-1", "vector_search", "{}")),
            _response(text="answer"),
        ]
    )

    await AgentService(chat, _CircularResultTools()).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1"),
    )

    tool_message = next(message for message in chat.requests[1].messages if message.role == "tool")
    assert "circular reference" in tool_message.content


def test_agent_tool_results_emit_strict_json_for_pathological_numbers() -> None:
    content = bounded_tool_result_content(
        {
            "nan": float("nan"),
            "infinity": float("inf"),
            "huge_integer": 1 << 100_000,
        }
    )

    assert '"nan":"<non-finite number>"' in content
    assert '"infinity":"<non-finite number>"' in content
    assert '"huge_integer":"<integer exceeds limit>"' in content
