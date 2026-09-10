"""Integration coverage for SQL-backed resumable agent-run checkpoints."""

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
    AgentToolExecution,
)
from harborrag_core.ports.conversation import ConversationIdentity

pytestmark = pytest.mark.integration


def _checkpoint(  # noqa: PLR0913 - test helper covers every checkpoint field explicitly
    identity: AgentRunIdentity,
    *,
    version: int,
    step: int = 1,
    status: AgentRunStatus = AgentRunStatus.RUNNING,
    stop_reason: AgentStopReason | None = None,
    response: HarborChatResponse | None = None,
    failure_retryable: bool = False,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
) -> AgentCheckpoint:
    now = datetime.now(UTC)
    return AgentCheckpoint(
        identity=identity,
        status=status,
        step=step,
        version=version,
        messages=(HarborChatMessage.user("multi-hop question"),),
        executions=(
            AgentToolExecution(
                step=1,
                call_id="call-1",
                tool="vector_search",
                ok=True,
                arguments_digest="deadbeef",
            ),
        ),
        usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        stop_reason=stop_reason,
        response=response,
        created_at=now,
        updated_at=now,
        failure_retryable=failure_retryable,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
    )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_agent_run_create_and_get_round_trip(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        await SqlConversationMemoryRepository(sessions).create(
            ConversationIdentity("ACME", "reader-1", "session-1", "user-1")
        )
        repo = SqlAgentRunRepository(sessions)
        identity = AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-1")
        checkpoint = _checkpoint(identity, version=1, step=0)

        await repo.create(checkpoint)
        loaded = await repo.get(identity)

        assert loaded is not None
        assert loaded.status is AgentRunStatus.RUNNING
        assert loaded.version == 1
        assert loaded.step == 0
        assert loaded.messages == checkpoint.messages
        assert loaded.executions == checkpoint.executions
        assert loaded.usage == checkpoint.usage
        assert loaded.stop_reason is None
        assert loaded.response is None
        assert loaded.failure_retryable is False
        assert loaded.lease_owner is None
        assert loaded.lease_expires_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_agent_run_save_step_advances_version_and_state(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        await SqlConversationMemoryRepository(sessions).create(
            ConversationIdentity("ACME", "reader-1", "session-1", "user-1")
        )
        repo = SqlAgentRunRepository(sessions)
        identity = AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-1")
        await repo.create(_checkpoint(identity, version=1, step=0))

        response = HarborChatResponse(
            id="resp-1",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="private",
            message=HarborChatMessage.assistant("final answer"),
            finish_reason="stop",
            usage=HarborChatUsage(prompt_tokens=2, completion_tokens=2, total_tokens=4),
        )
        await repo.save_step(
            _checkpoint(
                identity,
                version=2,
                step=1,
                status=AgentRunStatus.COMPLETED,
                stop_reason=AgentStopReason.FINAL_ANSWER,
                response=response,
            )
        )

        loaded = await repo.get(identity)
        assert loaded is not None
        assert loaded.version == 2
        assert loaded.step == 1
        assert loaded.status is AgentRunStatus.COMPLETED
        assert loaded.stop_reason is AgentStopReason.FINAL_ANSWER
        assert loaded.response is not None
        assert loaded.response.text == "final answer"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_agent_run_save_step_rejects_stale_version(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        await SqlConversationMemoryRepository(sessions).create(
            ConversationIdentity("ACME", "reader-1", "session-1", "user-1")
        )
        repo = SqlAgentRunRepository(sessions)
        identity = AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-1")
        await repo.create(_checkpoint(identity, version=1, step=0))
        await repo.save_step(_checkpoint(identity, version=2, step=1))

        with pytest.raises(HarborConflictError):
            # A second writer still believes version=1 is current (e.g. a racing
            # resume of an already-advanced run) and must be rejected, not silently
            # overwrite the newer state.
            await repo.save_step(_checkpoint(identity, version=2, step=1))

        loaded = await repo.get(identity)
        assert loaded is not None
        assert loaded.version == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_agent_run_save_step_rejects_mismatched_identity_even_with_correct_version(
    tmp_path: Path,
) -> None:
    """save_step used to filter only by run_id + version, unlike get()'s full
    identity scoping -- a caller that knew a run_id and the correct next
    version, but got tenant/user/session wrong, could still advance
    (or be blocked from advancing) someone else's run. Every mismatched
    field must behave exactly like a stale/unknown run: HarborConflictError,
    with the original row left untouched. ``principal_id`` is deliberately
    absent from that list: it is audit only, so the same human acting through
    a second credential still advances their own run (see
    test_agent_run_user_ownership.py)."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        await SqlConversationMemoryRepository(sessions).create(
            ConversationIdentity("ACME", "reader-1", "session-1", "user-1")
        )
        repo = SqlAgentRunRepository(sessions)
        identity = AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-1")
        await repo.create(_checkpoint(identity, version=1, step=0))

        for mismatched in (
            AgentRunIdentity("OTHER", "reader-1", "session-1", "run-1", "user-1"),
            AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-2"),
            AgentRunIdentity("ACME", "reader-1", "session-2", "run-1", "user-1"),
        ):
            with pytest.raises(HarborConflictError):
                await repo.save_step(_checkpoint(mismatched, version=2, step=1))

        loaded = await repo.get(identity)
        assert loaded is not None
        assert loaded.version == 1
        assert loaded.step == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_agent_run_get_is_scoped_to_full_identity(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        await SqlConversationMemoryRepository(sessions).create(
            ConversationIdentity("ACME", "reader-1", "session-1", "user-1")
        )
        repo = SqlAgentRunRepository(sessions)
        identity = AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-1")
        await repo.create(_checkpoint(identity, version=1, step=0))

        assert (
            await repo.get(AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-1"))
            is not None
        )
        assert (
            await repo.get(AgentRunIdentity("OTHER", "reader-1", "session-1", "run-1", "user-1"))
            is None
        )
        assert (
            await repo.get(AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-2"))
            is None
        )
        assert (
            await repo.get(AgentRunIdentity("ACME", "reader-1", "session-1", "missing", "user-1"))
            is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_agent_run_lease_and_failure_class_round_trip(tmp_path: Path) -> None:
    """Migration 0020 lease columns and the state_json failure flag must survive
    a save/load cycle with tz-aware datetimes, and a terminal save must clear
    the lease."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        await SqlConversationMemoryRepository(sessions).create(
            ConversationIdentity("ACME", "reader-1", "session-1", "user-1")
        )
        repo = SqlAgentRunRepository(sessions)
        identity = AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "user-1")
        expires = datetime(2030, 1, 1, 12, 30, tzinfo=UTC)
        await repo.create(
            _checkpoint(
                identity,
                version=1,
                step=0,
                lease_owner="host-a:42:abc",
                lease_expires_at=expires,
            )
        )

        loaded = await repo.get(identity)
        assert loaded is not None
        assert loaded.lease_owner == "host-a:42:abc"
        assert loaded.lease_expires_at == expires
        assert loaded.lease_active(datetime(2029, 12, 31, tzinfo=UTC)) is True
        assert loaded.lease_active(datetime(2030, 1, 2, tzinfo=UTC)) is False
        assert loaded.resumable(datetime(2029, 12, 31, tzinfo=UTC)) is False
        assert loaded.resumable(datetime(2030, 1, 2, tzinfo=UTC)) is True

        await repo.save_step(
            _checkpoint(
                identity,
                version=2,
                status=AgentRunStatus.FAILED,
                failure_retryable=True,
            )
        )
        failed = await repo.get(identity)
        assert failed is not None
        assert failed.status is AgentRunStatus.FAILED
        assert failed.failure_retryable is True
        assert failed.lease_owner is None
        assert failed.lease_expires_at is None
        assert failed.resumable(datetime.now(UTC)) is True
    finally:
        await engine.dispose()
