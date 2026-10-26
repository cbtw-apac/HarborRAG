"""The in-memory checkpoint repository never reopens a COMPLETED run."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.ports.agent_runs import AgentCheckpoint, AgentRunIdentity, AgentRunStatus
from harborrag_runtime.agent.checkpoint import InMemoryAgentRunRepository


def _checkpoint() -> AgentCheckpoint:
    now = datetime.now(UTC)
    return AgentCheckpoint(
        identity=AgentRunIdentity("tenant", "reader", "session", "run-1", "reader"),
        status=AgentRunStatus.RUNNING,
        step=0,
        version=1,
        messages=(),
        executions=(),
        usage=HarborChatUsage(),
        stop_reason=None,
        response=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_a_late_cancellation_cannot_reopen_a_completed_run() -> None:
    repository = InMemoryAgentRunRepository()
    created = _checkpoint()
    await repository.create(created)
    completed = replace(created, status=AgentRunStatus.COMPLETED, step=1, version=2)
    await repository.save_step(completed)

    # The next version, so only the terminal-status guard can refuse it.
    with pytest.raises(HarborConflictError, match="already completed"):
        await repository.save_step(replace(completed, status=AgentRunStatus.CANCELLED, version=3))

    assert await repository.get(created.identity) is completed
