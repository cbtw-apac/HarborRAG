"""Tests for the agent loop's guard bounds, lifecycle events, and resume path."""

from __future__ import annotations

import asyncio
import time

import pytest
from agent_test_helpers import (
    AlwaysSlowChat,
    Chat,
    Runs,
    SlowThenFastChat,
    Tools,
    many_tool_calls_response,
)
from agent_test_helpers import response as _response

from harborrag_core.models.chat import (
    HarborChatMessage,
)
from harborrag_core.ports.agent_runs import (
    AgentRunStatus,
    AgentStopReason,
)
from harborrag_engine.agent import AgentEvent, AgentRunOptions, AgentService


@pytest.mark.asyncio
async def test_agent_stops_on_repeated_identical_tool_call() -> None:
    tools = Tools()
    chat = Chat(
        [
            _response(call=("call-1", "vector_search_advanced", '{"query":"x"}')),
            _response(call=("call-2", "vector_search_advanced", '{"query":"x"}')),
            _response(text="repeat-safe answer"),
        ]
    )

    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            max_steps=4,
            max_repeated_tool_calls=1,
        ),
    )

    assert result.stop_reason is AgentStopReason.REPEATED_TOOL_CALL
    assert result.turns == 3
    assert result.response.text == "repeat-safe answer"
    assert "repeated" in chat.requests[2].messages[-1].content


@pytest.mark.asyncio
async def test_agent_rejects_same_turn_duplicates_before_dispatch() -> None:
    tools = Tools()
    chat = Chat([many_tool_calls_response(2, identical=True), _response(text="repeat-safe answer")])

    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            max_repeated_tool_calls=1,
        ),
    )

    assert len(tools.calls) == 1
    assert result.stop_reason is AgentStopReason.REPEATED_TOOL_CALL
    assert [execution.ok for execution in result.executions] == [True, False]


@pytest.mark.asyncio
async def test_agent_cancellation_persists_terminal_checkpoint() -> None:
    runs = Runs()
    service = AgentService(
        AlwaysSlowChat(delay=5, response=_response(text="never")),
        Tools(),
        runs=runs,
    )
    task = asyncio.create_task(
        service.run(
            [HarborChatMessage.user("question")],
            AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1"),
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    checkpoint = next(iter(runs.checkpoints.values()))
    assert checkpoint.status is AgentRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_completed_run_ignores_advisory_memory_and_event_failures() -> None:
    class BrokenMemory:
        async def recent(self, identity, *, limit=2):
            del identity, limit
            return ()

        async def append(self, identity, turn):
            del identity, turn
            raise RuntimeError("memory unavailable")

        async def clear(self, identity):
            del identity

    async def events(event: AgentEvent) -> None:
        if event.kind == "run.completed":
            raise RuntimeError("event sink unavailable")

    runs = Runs()
    result = await AgentService(
        Chat([_response(text="answer")]),
        Tools(),
        memory=BrokenMemory(),
        runs=runs,
    ).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1"),
        events=events,
    )

    assert result.response.text == "answer"
    assert runs.checkpoints[result.run_id].status is AgentRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_agent_stops_on_timeout_and_still_synthesizes() -> None:
    tools = Tools()
    chat = SlowThenFastChat(delay=0.05, response=_response(text="rushed answer"))

    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            max_steps=4,
            timeout_seconds=0.01,
        ),
    )

    assert result.stop_reason is AgentStopReason.TIMEOUT
    assert result.turns == 1
    assert result.response.text == "rushed answer"
    assert chat.calls == 2


@pytest.mark.asyncio
async def test_agent_synthesis_call_is_bounded_by_its_own_timeout() -> None:
    """The post-timeout synthesis call used to run with no guard at all, so a
    model that was slow on *every* call (not just the first, as in
    ``SlowThenFastChat``) would hang past the run's own deadline forever --
    exactly the failure mode the outer timeout exists to prevent. It must
    now fail fast via `synthesis_timeout_seconds`, not hang for the model's
    full delay."""
    tools = Tools()
    chat = AlwaysSlowChat(delay=5.0, response=_response(text="never seen"))

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await AgentService(chat, tools).run(
            [HarborChatMessage.user("question")],
            AgentRunOptions(
                tenant_id="ACME",
                principal_id="reader-1",
                session_id="session-1",
                max_steps=4,
                timeout_seconds=0.01,
                synthesis_timeout_seconds=0.05,
            ),
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert chat.calls == 2


@pytest.mark.asyncio
async def test_agent_stops_when_total_token_budget_is_exceeded() -> None:
    """Per-call caps (tool-result size, tool calls per turn, max_steps) are
    each individually bounded, but nothing previously capped their sum --
    a full-width multi-step run could still accumulate an unbounded amount
    of resent conversation and tool output. max_total_tokens is the
    backstop on the aggregate."""
    tools = Tools()
    chat = Chat([_response(text="must not be called")])

    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            max_steps=4,
            max_total_tokens=3,
        ),
    )

    assert result.stop_reason is AgentStopReason.TOKEN_BUDGET_EXCEEDED
    assert "token budget was exhausted" in result.response.text
    assert result.turns == 0
    assert result.usage.total_tokens == 0
    assert result.usage.total_tokens <= 3
    assert chat.requests == []
