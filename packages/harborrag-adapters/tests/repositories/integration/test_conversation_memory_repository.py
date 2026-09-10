"""Integration coverage for SQL-backed conversation memory."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command

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
from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    ConversationSessionRow,
)
from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMessage,
    ConversationRole,
    ConversationTurn,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_conversation_memory_returns_latest_two_isolated_turns(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    identity = ConversationIdentity("ACME", "principal-1", "session-1", "user-1")
    other = ConversationIdentity("ACME", "principal-1", "session-2", "user-1")
    try:
        await repo.create(identity)
        await repo.create(other)
        assert await repo.exists(identity) is True
        for index in range(3):
            turn = ConversationTurn(f"question-{index}", f"answer-{index}")
            await repo.append(identity, turn)
        await repo.append(other, ConversationTurn("other question", "other answer"))

        assert [turn.user_content for turn in await repo.recent(identity)] == [
            "question-1",
            "question-2",
        ]
        assert [turn.user_content for turn in await repo.recent(other)] == ["other question"]
        await repo.clear(identity)
        assert await repo.recent(identity) == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_conversation_sessions_are_bound_to_their_kind(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    chat = ConversationIdentity("ACME", "principal-1", "chat-session", "user-1")
    agent = ConversationIdentity("ACME", "principal-1", "agent-session", "user-1")
    try:
        await repo.create(chat)
        await repo.create(agent, kind="agent")

        assert await repo.exists(chat, kind="chat") is True
        assert await repo.exists(chat, kind="agent") is False
        assert await repo.exists(agent, kind="agent") is True
        assert await repo.exists(agent, kind="chat") is False
        assert await repo.exists(agent) is True
        # The credential is not the owner: the same human reaching their own
        # session through a second principal still finds it.
        other_principal = ConversationIdentity("ACME", "principal-2", "agent-session", "user-1")
        assert await repo.exists(other_principal) is True
        # A different human with the exact session id does not.
        other_user = ConversationIdentity("ACME", "principal-1", "agent-session", "user-2")
        assert await repo.exists(other_user) is False
    finally:
        await engine.dispose()


def _message(message_id: str, role: ConversationRole, content: str, **extra: object) -> Any:
    return ConversationMessage(
        message_id=message_id,
        role=role,
        content=content,
        created_at=datetime.now(UTC),
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_messages_round_trip_with_tool_and_citation_payloads(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    identity = ConversationIdentity("ACME", "principal-1", "session-1", "user-1")
    # Same tenant, same session id, different human: reads must come back empty.
    other = ConversationIdentity("ACME", "principal-1", "session-1", "user-2")
    try:
        await repo.create(identity)
        messages = (
            _message("m-1", "user", "what is the policy?", token_count=5),
            _message("m-2", "assistant", "", tool_calls_json='[{"name": "search"}]', run_id="r1"),
            _message("m-3", "tool", '{"hits": 2}', tool_call_id="call-1", run_id="r1"),
            _message("m-4", "assistant", "here it is", citations_json='["doc-1"]', run_id="r1"),
        )
        await repo.append_messages(identity, messages)

        stored = await repo.recent_messages(identity, limit=10)
        assert stored == messages
        assert await repo.recent_messages(identity, limit=2) == messages[2:]
        assert await repo.recent_messages(other, limit=10) == ()

        await repo.clear_messages(identity)
        assert await repo.recent_messages(identity, limit=10) == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_recent_turns_are_derived_from_messages(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    identity = ConversationIdentity("ACME", "principal-1", "session-1", "user-1")
    try:
        await repo.create(identity)
        await repo.append(identity, ConversationTurn("q-0", "a-0"))
        await repo.append_messages(
            identity,
            (
                _message("u-1", "user", "q-1"),
                _message("t-1", "assistant", "", tool_calls_json="[]"),
                _message("t-2", "tool", "result", tool_call_id="c"),
                _message("a-1", "assistant", "a-1"),
                _message("u-2", "user", "unanswered"),
            ),
        )

        # Turns pair a user message with the *next* assistant message; the
        # tool-call assistant stub (empty content) is that next message here.
        assert await repo.recent(identity, limit=5) == (
            ConversationTurn("q-0", "a-0"),
            ConversationTurn("q-1", ""),
        )
        assert await repo.recent(identity, limit=1) == (ConversationTurn("q-1", ""),)
        assert len(await repo.recent_messages(identity, limit=100)) == 7
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_messages_after_pages_oldest_first_and_rejects_foreign_cursor(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    identity = ConversationIdentity("ACME", "principal-1", "session-1", "user-1")
    other = ConversationIdentity("ACME", "principal-1", "session-2", "user-1")
    try:
        await repo.create(identity)
        await repo.create(other)
        for index in range(5):
            await repo.append_messages(identity, (_message(f"m-{index}", "user", str(index)),))
        await repo.append_messages(other, (_message("other-0", "user", "x"),))

        first = await repo.messages_after(identity, after_message_id=None, limit=2)
        assert [m.message_id for m in first] == ["m-0", "m-1"]
        second = await repo.messages_after(identity, after_message_id="m-1", limit=2)
        assert [m.message_id for m in second] == ["m-2", "m-3"]
        third = await repo.messages_after(identity, after_message_id="m-3", limit=2)
        assert [m.message_id for m in third] == ["m-4"]
        assert await repo.messages_after(identity, after_message_id="m-4", limit=2) == ()

        with pytest.raises(ValueError, match="unknown conversation message cursor"):
            await repo.messages_after(identity, after_message_id="other-0", limit=2)
        with pytest.raises(ValueError, match="limit must be positive"):
            await repo.messages_after(identity, after_message_id=None, limit=0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_messages_cascade_when_their_session_is_deleted(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    repo = SqlConversationMemoryRepository(sessions)
    identity = ConversationIdentity("ACME", "principal-1", "session-1", "user-1")
    try:
        await repo.create(identity)
        await repo.append(identity, ConversationTurn("q", "a"))

        async with sessions.begin() as session:
            # SQLite only enforces ON DELETE CASCADE with the pragma on.
            await session.execute(sa.text("PRAGMA foreign_keys=ON"))
            await session.execute(
                sa.delete(ConversationSessionRow).where(
                    ConversationSessionRow.session_id == identity.session_id
                )
            )

        assert await repo.exists(identity) is False
        assert await repo.recent_messages(identity, limit=10) == ()
    finally:
        await engine.dispose()


@pytest.mark.whitebox
def test_migration_0022_backfills_legacy_pairs_as_messages(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    config = _build_config(dsn)
    command.upgrade(config, "0021")

    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    created_at = datetime(2026, 8, 10, 5, 0, tzinfo=UTC)
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO conversation_sessions "
                    "(session_id, tenant_id, principal_id, kind, created_at) "
                    "VALUES ('s-1', 'ACME', 'p-1', 'chat', :created_at)"
                ),
                {"created_at": created_at},
            )
            for index in range(2):
                connection.execute(
                    sa.text(
                        "INSERT INTO conversation_memory "
                        "(tenant_id, principal_id, session_id, user_content, "
                        "assistant_content, created_at) VALUES "
                        "('ACME', 'p-1', 's-1', :user, :assistant, :created_at)"
                    ),
                    {"user": f"q-{index}", "assistant": f"a-{index}", "created_at": created_at},
                )

        command.upgrade(config, "head")

        with sync_engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT message_id, role, content, seq FROM conversation_messages ORDER BY seq"
                )
            ).all()
            tables = set(sa.inspect(connection).get_table_names())
        assert [tuple(row) for row in rows] == [
            ("legacy-1-user", "user", "q-0", 2),
            ("legacy-1-assistant", "assistant", "a-0", 3),
            ("legacy-2-user", "user", "q-1", 4),
            ("legacy-2-assistant", "assistant", "a-1", 5),
        ]
        assert "conversation_memory_legacy" in tables
        assert "conversation_memory" not in tables
    finally:
        sync_engine.dispose()

    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    # 0024 backfills user_id from principal_id, so the migrated rows are
    # reachable under user_id == "p-1" and nothing else.
    identity = ConversationIdentity("ACME", "p-1", "s-1", "p-1")

    async def _read_and_extend() -> tuple[ConversationTurn, ...]:
        await repo.append(identity, ConversationTurn("q-2", "a-2"))
        return await repo.recent(identity, limit=5)

    try:
        assert asyncio.run(_read_and_extend()) == (
            ConversationTurn("q-0", "a-0"),
            ConversationTurn("q-1", "a-1"),
            ConversationTurn("q-2", "a-2"),
        )
    finally:
        asyncio.run(engine.dispose())

    # Downgrade re-pairs the post-upgrade turn into the restored legacy table.
    command.downgrade(config, "0021")
    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    try:
        with sync_engine.connect() as connection:
            pairs = connection.execute(
                sa.text(
                    "SELECT user_content, assistant_content FROM conversation_memory ORDER BY id"
                )
            ).all()
            tables = set(sa.inspect(connection).get_table_names())
        assert [tuple(row) for row in pairs] == [("q-0", "a-0"), ("q-1", "a-1"), ("q-2", "a-2")]
        assert "conversation_messages" not in tables
    finally:
        sync_engine.dispose()
