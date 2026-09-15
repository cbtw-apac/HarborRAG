"""Runtime checkpoint repositories preserve isolation and optimistic concurrency."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_core.contracts.errors import HarborConfigurationError, HarborConflictError
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunStatus,
)
from harborrag_runtime.agent.checkpoint import (
    DatabaseAgentRunRepository,
    InMemoryAgentRunRepository,
)
from harborrag_runtime.config.settings import RuntimeSettings


def _checkpoint(run_id: str, *, version: int = 1) -> AgentCheckpoint:
    now = datetime.now(UTC)
    return AgentCheckpoint(
        identity=AgentRunIdentity("tenant", "reader", "session", run_id),
        status=AgentRunStatus.RUNNING,
        step=version - 1,
        version=version,
        messages=(),
        executions=(),
        usage=HarborChatUsage(),
        stop_reason=None,
        response=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_in_memory_checkpoints_are_bounded_isolated_and_versioned() -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        InMemoryAgentRunRepository(max_runs=0)

    repository = InMemoryAgentRunRepository(max_runs=1)
    first = _checkpoint("first")
    await repository.create(first)
    assert await repository.get(first.identity) is first
    assert await repository.get(AgentRunIdentity("other", "reader", "session", "first")) is None
    assert await repository.get(AgentRunIdentity("tenant", "reader", "session", "missing")) is None

    advanced = replace(first, step=1, version=2)
    await repository.save_step(advanced)
    assert await repository.get(first.identity) is advanced
    with pytest.raises(HarborConflictError, match="version conflict"):
        await repository.save_step(advanced)

    second = _checkpoint("second")
    await repository.create(second)
    assert await repository.get(first.identity) is None
    assert await repository.get(second.identity) is second


def test_database_checkpoints_require_an_explicit_production_dsn() -> None:
    settings = RuntimeSettings().model_copy(update={"env": "prod"})
    with pytest.raises(HarborConfigurationError, match="HARBORRAG_CONTROL_DB_URL"):
        DatabaseAgentRunRepository.configured(settings)


@pytest.mark.asyncio
async def test_database_checkpoint_composition_and_delegation(monkeypatch) -> None:
    from harborrag_adapters.repositories.database.control_plane import (
        agent_runs,
        engine,
        migrations,
    )

    migrated = Mock()
    database_engine = SimpleNamespace(dispose=AsyncMock())
    session_factory = object()
    repository = SimpleNamespace(
        create=AsyncMock(),
        save_step=AsyncMock(),
        get=AsyncMock(return_value="stored"),
    )
    monkeypatch.setattr(migrations, "run_migrations", migrated)
    monkeypatch.setattr(engine, "create_control_plane_engine", Mock(return_value=database_engine))
    monkeypatch.setattr(engine, "create_session_factory", Mock(return_value=session_factory))
    monkeypatch.setattr(agent_runs, "SqlAgentRunRepository", Mock(return_value=repository))

    configured = DatabaseAgentRunRepository.configured(RuntimeSettings())
    checkpoint = _checkpoint("database")
    await configured.create(checkpoint)
    await configured.save_step(checkpoint)
    assert await configured.get(checkpoint.identity) == "stored"
    await configured.aclose()

    migrated.assert_called_once_with("sqlite+aiosqlite:///./harborrag_control.db")
    repository.create.assert_awaited_once_with(checkpoint)
    repository.save_step.assert_awaited_once_with(checkpoint)
    repository.get.assert_awaited_once_with(checkpoint.identity)
    database_engine.dispose.assert_awaited_once()
