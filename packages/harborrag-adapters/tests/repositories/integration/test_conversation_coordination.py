"""Database guarantees for conversation sequencing, leases, and title assignment."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from harborrag_adapters.repositories.database.control_plane.conversation import (
    SqlConversationMemoryRepository,
)
from harborrag_adapters.repositories.database.control_plane.migrations import _build_config
from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    ConversationMessageRow,
    ConversationSessionRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]


@pytest.mark.asyncio
async def test_concurrent_appends_reserve_contiguous_unique_sequences(
    sessions: SessionFactory,
) -> None:
    repo = SqlConversationMemoryRepository(sessions)
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await repo.create(identity)

    async def append(index: int) -> None:
        await repo.append_messages(
            identity,
            tuple(
                ConversationMessage(f"{index}-{offset}", "user", str(index), datetime.now(UTC))
                for offset in range(2)
            ),
        )

    await asyncio.gather(*(append(index) for index in range(20)))
    async with sessions() as session:
        rows = list(
            await session.scalars(
                sa.select(ConversationMessageRow).order_by(ConversationMessageRow.seq)
            )
        )
    assert [row.seq for row in rows] == list(range(1, 41))
    assert all(rows[index].content == rows[index + 1].content for index in range(0, 40, 2))
    await repo.clear_messages(identity)
    await append(21)
    async with sessions() as session:
        assert await session.scalar(sa.select(sa.func.min(ConversationMessageRow.seq))) == 41


@pytest.mark.asyncio
async def test_append_cannot_write_into_another_users_session(sessions: SessionFactory) -> None:
    repo = SqlConversationMemoryRepository(sessions)
    await repo.create(ConversationIdentity("tenant", "principal", "session", "owner"))
    impostor = ConversationIdentity("tenant", "principal", "session", "other-user")
    with pytest.raises(ValueError, match="session does not exist"):
        await repo.append_messages(
            impostor, (ConversationMessage("message", "user", "content", datetime.now(UTC)),)
        )


@pytest.mark.asyncio
async def test_recent_complete_messages_skips_partial_and_tool_turns(
    sessions: SessionFactory,
) -> None:
    repo = SqlConversationMemoryRepository(sessions)
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await repo.create(identity)
    now = datetime.now(UTC)
    messages = [
        message
        for index in range(4)
        for message in (
            ConversationMessage(f"u-{index}", "user", f"question {index}", now),
            ConversationMessage(
                f"call-{index}", "assistant", "Searching", now, tool_calls_json="[{}]"
            ),
            ConversationMessage(f"t-{index}", "tool", "search result", now),
            ConversationMessage(f"a-{index}", "assistant", f"answer {index}", now),
        )
    ]
    # More than six trailing messages must not crowd out the three real pairs.
    messages.extend(
        message
        for index in range(5)
        for message in (
            ConversationMessage(f"pending-{index}", "user", "unfinished", now),
            ConversationMessage(f"partial-{index}", "assistant", "interrupted", now, partial=True),
        )
    )
    await repo.append_messages(identity, messages)
    assert [message.message_id for message in await repo.recent_complete_messages(identity)] == [
        "u-1",
        "a-1",
        "u-2",
        "a-2",
        "u-3",
        "a-3",
    ]


@pytest.mark.asyncio
async def test_generated_title_is_assigned_once_and_manual_clear_wins(
    sessions: SessionFactory,
) -> None:
    repo = SqlConversationMemoryRepository(sessions)
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await repo.create(identity)
    assigned = await asyncio.gather(
        repo.set_generated_title(identity, title="First"),
        repo.set_generated_title(identity, title="Second"),
    )
    assert sum(assigned) == 1
    assert await repo.get_title(identity) in {"First", "Second"}
    assert await repo.rename_conversation(identity, title="")
    assert not await repo.set_generated_title(identity, title="Should not replace the clear")
    assert await repo.get_title(identity) is None
    stranger = ConversationIdentity("tenant", "principal", "session", "stranger")
    assert not await repo.set_generated_title(stranger, title="Foreign title")


@pytest.mark.asyncio
async def test_turn_lease_is_exclusive_renewable_and_owner_scoped(sessions: SessionFactory) -> None:
    repo = SqlConversationMemoryRepository(sessions)
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await repo.create(identity)
    claims = await asyncio.gather(
        *(
            repo.acquire_turn_lease(identity, token=f"worker-{i}", lease_seconds=60)
            for i in range(5)
        )
    )
    assert sum(claims) == 1
    winner = f"worker-{claims.index(True)}"
    assert await repo.renew_turn_lease(identity, token=winner, lease_seconds=120)
    assert not await repo.renew_turn_lease(identity, token="other", lease_seconds=120)
    await repo.release_turn_lease(identity, token="other")
    assert not await repo.acquire_turn_lease(identity, token="other", lease_seconds=60)
    async with sessions.begin() as session:
        await session.execute(
            sa.update(ConversationSessionRow).values(
                turn_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    assert not await repo.renew_turn_lease(identity, token=winner, lease_seconds=120)
    assert await repo.acquire_turn_lease(identity, token="successor", lease_seconds=60)
    await repo.release_turn_lease(identity, token=winner)
    assert not await repo.acquire_turn_lease(identity, token="other", lease_seconds=60)
    await repo.release_turn_lease(identity, token="successor")
    stranger = ConversationIdentity("tenant", "principal", "session", "stranger")
    assert not await repo.acquire_turn_lease(stranger, token="foreign", lease_seconds=60)


@pytest.mark.asyncio
async def test_stale_finalizers_cannot_write_after_lease_takeover(sessions: SessionFactory) -> None:
    original = SqlConversationMemoryRepository(sessions)
    successor = SqlConversationMemoryRepository(sessions)
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await original.create(identity)
    assert await original.acquire_turn_lease(identity, token="original", lease_seconds=60)
    release_finalizer = asyncio.Event()

    async def finalize_old_turn() -> None:
        await release_finalizer.wait()
        with pytest.raises(HarborConflictError):
            await original.append_messages(
                identity, (ConversationMessage("stale", "assistant", "late", datetime.now(UTC)),)
            )

    finalizer = asyncio.create_task(finalize_old_turn())
    async with sessions.begin() as session:
        await session.execute(
            sa.update(ConversationSessionRow).values(
                turn_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    assert await successor.acquire_turn_lease(identity, token="successor", lease_seconds=60)
    await successor.append_messages(
        identity, (ConversationMessage("current", "assistant", "current", datetime.now(UTC)),)
    )
    release_finalizer.set()
    await finalizer
    assert [
        message.message_id for message in await successor.recent_messages(identity, limit=10)
    ] == ["current"]


@pytest.mark.asyncio
async def test_foreign_delete_and_clear_are_blocked_during_an_active_turn(
    sessions: SessionFactory,
) -> None:
    completion = SqlConversationMemoryRepository(sessions)
    erasure = SqlConversationMemoryRepository(sessions)
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    await completion.create(identity)
    await completion.append_messages(
        identity, (ConversationMessage("private", "user", "private content", datetime.now(UTC)),)
    )
    assert await completion.acquire_turn_lease(identity, token="active", lease_seconds=60)
    for operation in (erasure.delete, erasure.clear_messages):
        with pytest.raises(HarborConflictError):
            await operation(identity)
    await completion.release_turn_lease(identity, token="active")
    assert await erasure.delete(identity)
    assert await erasure.recent_messages(identity, limit=10) == ()


def test_migration_repairs_existing_sequence_ties_in_reader_order(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/legacy.db"
    config = _build_config(dsn)
    command.upgrade(config, "0032")
    engine = sa.create_engine(f"sqlite:///{tmp_path}/legacy.db")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """INSERT INTO conversation_sessions
                    (session_id, tenant_id, principal_id, user_id, kind, created_at, updated_at)
                    VALUES ('session', 'tenant', 'principal', 'user', 'chat', :now, :now)"""
                ),
                {"now": datetime.now(UTC)},
            )
            connection.execute(
                sa.text(
                    """INSERT INTO conversation_messages
                    (message_id, tenant_id, principal_id, user_id, session_id, role, content,
                     created_at, seq, partial)
                    VALUES (:message, 'tenant', 'principal', 'user', 'session', 'user',
                            'content', :now, 1, 0)"""
                ),
                [
                    {"message": message, "now": datetime(2025, 1, 1, tzinfo=UTC)}
                    for message in ("b", "a")
                ],
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT message_id, seq FROM conversation_messages ORDER BY seq")
            ).all() == [("a", 1), ("b", 2)]
            assert (
                connection.scalar(sa.text("SELECT next_message_seq FROM conversation_sessions"))
                == 3
            )
    finally:
        engine.dispose()
