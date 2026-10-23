"""A COMPLETED agent run is never moved to another status by ``save_step``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.control_plane.agent_runs import (
    SqlAgentRunRepository,
)
from harborrag_adapters.repositories.database.control_plane.conversation import (
    SqlConversationMemoryRepository,
)
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunStatus,
    AgentStopReason,
)
from harborrag_core.ports.conversation import ConversationIdentity

pytestmark = pytest.mark.integration

_IDENTITY = AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-1")


def _checkpoint(
    *,
    version: int,
    status: AgentRunStatus,
    response: HarborChatResponse | None = None,
) -> AgentCheckpoint:
    now = datetime.now(UTC)
    return AgentCheckpoint(
        identity=_IDENTITY,
        status=status,
        step=1,
        version=version,
        messages=(HarborChatMessage.user("question"),),
        executions=(),
        usage=HarborChatUsage(),
        stop_reason=None if response is None else AgentStopReason.FINAL_ANSWER,
        response=response,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_a_late_cancellation_cannot_reopen_a_completed_run(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        await SqlConversationMemoryRepository(sessions).create(
            ConversationIdentity("ACME", "reader-1", "session-1", "user-1")
        )
        repo = SqlAgentRunRepository(sessions)
        await repo.create(_checkpoint(version=1, status=AgentRunStatus.RUNNING))
        answer = HarborChatResponse(
            id="resp-1",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="private",
            message=HarborChatMessage.assistant("final answer"),
            finish_reason="stop",
            usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
        await repo.save_step(
            _checkpoint(version=2, status=AgentRunStatus.COMPLETED, response=answer)
        )

        # The next version, so only the terminal-status guard can refuse it.
        with pytest.raises(HarborConflictError, match="already completed"):
            await repo.save_step(_checkpoint(version=3, status=AgentRunStatus.CANCELLED))

        loaded = await repo.get(_IDENTITY)
        assert loaded is not None
        assert loaded.status is AgentRunStatus.COMPLETED
        assert loaded.version == 2
        assert loaded.response is not None
        assert loaded.response.text == "final answer"
    finally:
        await engine.dispose()
