"""Agent-run checkpoints belong to the human, not to the credential.

``conversation_sessions`` became user-owned in migration 0024 while
``agent_runs`` -- its foreign-key child -- still filtered
``(tenant_id, principal_id, session_id)``. Two humans behind one service
principal could therefore read, checkpoint into, and resume each other's
runs. These tests pin the closed predicate: user in, principal out.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine

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
from harborrag_adapters.repositories.database.control_plane.migrations import (
    _build_config,
    run_migrations,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.models.chat import HarborChatMessage, HarborChatUsage
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunStatus,
    AgentToolExecution,
)
from harborrag_core.ports.conversation import ConversationIdentity

pytestmark = pytest.mark.integration

_OWNER = AgentRunIdentity("ACME", "shared-principal", "session-1", "run-1", "user-a")
_LEASE_EXPIRES = datetime(2030, 1, 1, 12, 30, tzinfo=UTC)


def _checkpoint(
    identity: AgentRunIdentity,
    *,
    version: int,
    step: int = 0,
    status: AgentRunStatus = AgentRunStatus.RUNNING,
    lease_owner: str | None = "host-a:1:abc",
) -> AgentCheckpoint:
    now = datetime.now(UTC)
    return AgentCheckpoint(
        identity=identity,
        status=status,
        step=step,
        version=version,
        messages=(HarborChatMessage.user("private question"),),
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
        stop_reason=None,
        response=None,
        created_at=now,
        updated_at=now,
        lease_owner=lease_owner,
        lease_expires_at=_LEASE_EXPIRES if lease_owner is not None else None,
    )


async def _seed_owner_run(dsn: str) -> tuple[SqlAgentRunRepository, AsyncEngine]:
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    await SqlConversationMemoryRepository(sessions).create(
        ConversationIdentity("ACME", "shared-principal", "session-1", "user-a")
    )
    repo = SqlAgentRunRepository(sessions)
    await repo.create(_checkpoint(_OWNER, version=1))
    return repo, engine


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_second_user_behind_the_same_principal_cannot_read_or_advance_the_run(
    tmp_path: Path,
) -> None:
    """The shared-principal leak: same tenant, same credential, same session
    id, same run id -- only the human differs, and that must be enough."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    repo, engine = await _seed_owner_run(dsn)
    try:
        intruder = AgentRunIdentity("ACME", "shared-principal", "session-1", "run-1", "user-b")

        assert await repo.get(intruder) is None
        with pytest.raises(HarborConflictError):
            await repo.save_step(_checkpoint(intruder, version=2, step=1))

        untouched = await repo.get(_OWNER)
        assert untouched is not None
        assert untouched.version == 1
        assert untouched.step == 0
        assert untouched.messages[0].content == "private question"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_same_user_through_a_second_credential_still_owns_the_run(
    tmp_path: Path,
) -> None:
    """``principal_id`` is audit only: the same human re-authenticating through
    another credential (a rotated key, a second client) keeps their run, and
    the lease and optimistic-version guard behave exactly as before."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    repo, engine = await _seed_owner_run(dsn)
    try:
        rotated = AgentRunIdentity("ACME", "other-principal", "session-1", "run-1", "user-a")

        loaded = await repo.get(rotated)
        assert loaded is not None
        # The stored credential is still the one that created the run.
        assert loaded.identity.principal_id == "shared-principal"
        assert loaded.identity.user_id == "user-a"
        assert loaded.lease_owner == "host-a:1:abc"
        assert loaded.lease_expires_at == _LEASE_EXPIRES
        assert loaded.lease_active(datetime(2029, 12, 31, tzinfo=UTC)) is True

        await repo.save_step(_checkpoint(rotated, version=2, step=1))
        with pytest.raises(HarborConflictError):
            # Version guard unchanged: a writer still at version 1 is stale.
            await repo.save_step(_checkpoint(rotated, version=2, step=1))

        advanced = await repo.get(_OWNER)
        assert advanced is not None
        assert advanced.version == 2
        assert advanced.step == 1

        terminal = _checkpoint(
            rotated, version=3, step=1, status=AgentRunStatus.COMPLETED, lease_owner=None
        )
        await repo.save_step(terminal)
        released = await repo.get(_OWNER)
        assert released is not None
        assert released.status is AgentRunStatus.COMPLETED
        assert released.lease_owner is None
        assert released.lease_expires_at is None
    finally:
        await engine.dispose()


_SEED_SESSION = sa.text(
    "INSERT INTO conversation_sessions "
    "(session_id, tenant_id, principal_id, user_id, kind, created_at, updated_at) "
    "VALUES ('session-legacy', 'ACME', 'legacy-principal', 'legacy-principal', "
    "'agent', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
)
_SEED_RUN = sa.text(
    "INSERT INTO agent_runs "
    "(run_id, tenant_id, principal_id, session_id, status, step, version, "
    "state_json, created_at, updated_at) "
    "VALUES ('run-legacy', 'ACME', 'legacy-principal', 'session-legacy', 'running', "
    "1, 1, '{}', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
)


@pytest.mark.whitebox
def test_migration_0027_backfills_user_id_from_principal_id(tmp_path: Path) -> None:
    """A database stamped at 0026 has runs scoped only by their credential, so
    that credential is their owner: the backfill must adopt it verbatim and
    then make the column mandatory."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    config = _build_config(dsn)
    command.upgrade(config, "0026")

    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    try:
        with sync_engine.begin() as connection:
            assert "user_id" not in {
                column["name"] for column in sa.inspect(sync_engine).get_columns("agent_runs")
            }
            connection.execute(_SEED_SESSION)
            connection.execute(_SEED_RUN)
    finally:
        sync_engine.dispose()

    command.upgrade(config, "head")

    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    try:
        with sync_engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT principal_id, user_id FROM agent_runs WHERE run_id = 'run-legacy'")
            ).one()
            assert row.user_id == "legacy-principal"
            assert row.user_id == row.principal_id
        inspector = sa.inspect(sync_engine)
        user_id = next(
            column for column in inspector.get_columns("agent_runs") if column["name"] == "user_id"
        )
        assert user_id["nullable"] is False
        index_names = {index["name"] for index in inspector.get_indexes("agent_runs")}
        assert "ix_agent_runs_user_session" in index_names
        assert "ix_agent_runs_tenant_id" not in index_names
    finally:
        sync_engine.dispose()
