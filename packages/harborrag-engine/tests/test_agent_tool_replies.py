"""Tests that the agent transcript never dangles tool calls and ends with the answer."""

from __future__ import annotations

import asyncio
import json

import pytest
from agent_test_helpers import Chat, Runs, SlowTools, Tools, many_tool_calls_response
from agent_test_helpers import response as _response

from harborrag_core.models.chat import HarborChatMessage, MessageRole
from harborrag_core.ports.agent_runs import AgentRunStatus, AgentStopReason
from harborrag_engine.agent import AgentRunOptions, AgentService

_OPTIONS = AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1")


def _assert_no_dangling_tool_calls(messages) -> None:
    replied = {m.tool_call_id for m in messages if m.role is MessageRole.TOOL}
    issued = {call.id for m in messages if m.role is MessageRole.ASSISTANT for call in m.tool_calls}
    assert issued, "scenario must issue at least one tool call"
    assert issued <= replied


@pytest.mark.asyncio
async def test_tool_gather_timeout_replies_to_every_admitted_call_before_synthesis() -> None:
    chat = Chat([many_tool_calls_response(10), _response(text="rushed answer")])
    runs = Runs()

    result = await AgentService(chat, SlowTools(delay=5.0), runs=runs).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            timeout_seconds=0.05,
        ),
    )

    assert result.stop_reason is AgentStopReason.TIMEOUT
    assert result.response.text == "rushed answer"
    synthesis_messages = chat.requests[1].messages
    _assert_no_dangling_tool_calls(synthesis_messages)
    tool_replies = [m for m in synthesis_messages if m.role is MessageRole.TOOL]
    assert len(tool_replies) == 10
    timed_out = [json.loads(m.content) for m in tool_replies[:8]]
    assert timed_out == [{"error": "tool call timed out", "ok": False}] * 8
    overflow = [json.loads(m.content) for m in tool_replies[8:]]
    assert all(reply["error"] == "tool call budget exceeded for this turn" for reply in overflow)
    assert len(result.executions) == 10
    assert not any(execution.ok for execution in result.executions)
    persisted = runs.checkpoints[result.run_id]
    assert persisted.status is AgentRunStatus.COMPLETED
    _assert_no_dangling_tool_calls(persisted.messages)


@pytest.mark.asyncio
async def test_tool_phase_exception_replies_to_calls_before_failing_the_run() -> None:
    class ExplodingTools(Tools):
        async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
            raise RuntimeError("boom")

    class BrokenGatherService(AgentService):
        pass

    # ``_invoke`` swallows tool exceptions into model-visible data, so force
    # the gather itself to fail by breaking the executor's tool-call method.
    chat = Chat([many_tool_calls_response(2)])
    runs = Runs()
    service = BrokenGatherService(chat, ExplodingTools(), runs=runs)

    async def exploding_gather(*args, **kwargs):
        raise RuntimeError("tool transport exploded")

    service._loop._executor.execute_tool_calls = exploding_gather  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="tool transport exploded"):
        await service.run([HarborChatMessage.user("question")], _OPTIONS)

    persisted = next(iter(runs.checkpoints.values()))
    assert persisted.status is AgentRunStatus.FAILED
    assert persisted.failure_retryable is False
    _assert_no_dangling_tool_calls(persisted.messages)
    assert json.loads(persisted.messages[-1].content) == {"error": "tool call failed", "ok": False}


@pytest.mark.asyncio
async def test_cancelling_a_run_mid_tool_call_leaves_no_dangling_tool_call() -> None:
    """A CANCELLED checkpoint has to stay resumable, so it may dangle nothing.

    ``CancelledError`` is a ``BaseException``: it matched neither the timeout
    handler nor the broad one, so the shielded cancellation checkpoint saved an
    assistant message whose tool calls had no replies. Resuming that run
    replayed a ``tool_call_id`` the provider rejects for good.
    """

    entered = asyncio.Event()

    class BlockingTools(Tools):
        async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
            entered.set()
            await asyncio.Event().wait()

    chat = Chat([many_tool_calls_response(3)])
    runs = Runs()
    service = AgentService(chat, BlockingTools(), runs=runs)

    task = asyncio.create_task(service.run([HarborChatMessage.user("question")], _OPTIONS))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    persisted = next(iter(runs.checkpoints.values()))
    assert persisted.status is AgentRunStatus.CANCELLED
    _assert_no_dangling_tool_calls(persisted.messages)
    assert json.loads(persisted.messages[-1].content) == {
        "error": "tool call cancelled",
        "ok": False,
    }


@pytest.mark.asyncio
async def test_completed_checkpoint_ends_with_assistant_answer_after_tool_use() -> None:
    chat = Chat(
        [
            _response(call=("call-1", "vector_search", '{"query":"x"}')),
            _response(text="final answer"),
        ]
    )
    runs = Runs()

    result = await AgentService(chat, Tools(), runs=runs).run(
        [HarborChatMessage.user("question")], _OPTIONS
    )

    persisted = runs.checkpoints[result.run_id]
    assert persisted.status is AgentRunStatus.COMPLETED
    assert [m.role for m in persisted.messages[-3:]] == [
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert persisted.messages[-1].content == "final answer"
    assert persisted.messages[-1] == result.response.message
    _assert_no_dangling_tool_calls(persisted.messages)


@pytest.mark.asyncio
async def test_completed_checkpoint_ends_with_synthesized_answer_after_step_budget() -> None:
    chat = Chat(
        [
            _response(call=("call-1", "vector_search", '{"query":"x"}')),
            _response(text="budgeted answer"),
        ]
    )
    runs = Runs()

    result = await AgentService(chat, Tools(), runs=runs).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME", principal_id="reader-1", session_id="session-1", max_steps=1
        ),
    )

    assert result.stop_reason is AgentStopReason.MAX_STEPS
    persisted = runs.checkpoints[result.run_id]
    assert persisted.messages[-2].role is MessageRole.DEVELOPER
    assert persisted.messages[-1].role is MessageRole.ASSISTANT
    assert persisted.messages[-1].content == "budgeted answer"
