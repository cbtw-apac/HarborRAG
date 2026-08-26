"""Tests for durable agent lifecycle events and resume behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from agent_test_helpers import Chat, Runs, Tools
from agent_test_helpers import response as _response

from harborrag_core.contracts.errors import HarborConfigurationError, HarborNotFoundError
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatUsage,
    HarborToolCall,
    HarborToolCallFunction,
)
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunStatus,
    AgentStopReason,
    AgentToolExecution,
)
from harborrag_engine.agent import AgentEvent, AgentRunOptions, AgentService
from harborrag_engine.agent.guard import digest_arguments


@pytest.mark.asyncio
async def test_agent_emits_lifecycle_events_in_order() -> None:
    tools = Tools()
    chat = Chat(
        [
            _response(call=("call-1", "vector_search", '{"query":"x"}')),
            _response(text="final answer"),
        ]
    )
    events: list[AgentEvent] = []

    async def sink(event: AgentEvent) -> None:
        events.append(event)

    result = await AgentService(chat, tools).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
        ),
        events=sink,
    )

    assert [event.kind for event in events] == [
        "run.started",
        "agent.step.started",
        "tool.started",
        "tool.completed",
        "agent.step.completed",
        "agent.step.started",
        "run.completed",
    ]
    assert all(event.run_id == result.run_id for event in events)


@pytest.mark.asyncio
async def test_agent_resume_continues_from_last_checkpoint() -> None:
    identity = AgentRunIdentity(
        tenant_id="ACME",
        principal_id="reader-1",
        session_id="session-1",
        run_id="run-fixed",
    )
    prior_digest = digest_arguments({"query": "x"})
    now = datetime.now(UTC)
    checkpoint = AgentCheckpoint(
        identity=identity,
        status=AgentRunStatus.RUNNING,
        step=1,
        version=2,
        messages=(
            HarborChatMessage.user("multi-hop question"),
            HarborChatMessage.assistant(
                None,
                tool_calls=(
                    HarborToolCall(
                        id="call-1",
                        function=HarborToolCallFunction(
                            name="vector_search",
                            arguments='{"query":"x"}',
                            parsed_arguments={"query": "x"},
                        ),
                    ),
                ),
            ),
            HarborChatMessage.tool(
                json.dumps({"ok": True}),
                tool_call_id="call-1",
                name="vector_search",
            ),
        ),
        executions=(
            AgentToolExecution(
                step=1,
                call_id="call-1",
                tool="vector_search",
                ok=True,
                arguments_digest=prior_digest,
            ),
        ),
        usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        stop_reason=None,
        response=None,
        created_at=now,
        updated_at=now,
    )
    runs = Runs()
    runs.checkpoints[identity.run_id] = checkpoint
    tools = Tools()
    chat = Chat([_response(text="resumed answer")])
    service = AgentService(chat, tools, runs=runs)

    result = await service.resume(
        "run-fixed",
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            max_steps=3,
        ),
    )

    assert result.run_id == "run-fixed"
    assert result.response.text == "resumed answer"
    assert result.stop_reason is AgentStopReason.FINAL_ANSWER
    assert result.turns == 2
    assert len(result.executions) == 1
    persisted = runs.checkpoints["run-fixed"]
    assert persisted.status is AgentRunStatus.COMPLETED
    assert persisted.version == 3
    assert chat.requests[0].messages[0].content == "multi-hop question"


@pytest.mark.asyncio
async def test_agent_resume_rejects_unknown_or_non_running_run() -> None:
    runs = Runs()
    service = AgentService(Chat([]), Tools(), runs=runs)

    with pytest.raises(HarborNotFoundError):
        await service.resume(
            "missing-run",
            AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1"),
        )


@pytest.mark.asyncio
async def test_agent_resume_requires_configured_run_repository() -> None:
    service = AgentService(Chat([]), Tools())

    with pytest.raises(HarborConfigurationError):
        await service.resume(
            "run-x",
            AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1"),
        )
