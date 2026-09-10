"""Conversation history is owned by the human, not by the credential.

One service principal fronting several people used to leave ``session_id`` as
the only separator between their histories, so a known or guessed id replayed
somebody else's conversation into the prompt. These tests pin the fix from
both directions: a second user is locked out even with the same principal and
the exact session id, while the same user still reaches their own session
through a different credential.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
    ConversationTurn,
    new_message_id,
)

pytestmark = pytest.mark.integration

OWNER = ConversationIdentity("ACME", "shared-principal", "session-1", "user-1")
# Same tenant, same credential, the exact session id -- a different human.
INTRUDER = ConversationIdentity("ACME", "shared-principal", "session-1", "user-2")
# Same human, a second credential: still their own conversation.
SECOND_CREDENTIAL = ConversationIdentity("ACME", "other-principal", "session-1", "user-1")


def _message(message_id: str, content: str) -> ConversationMessage:
    return ConversationMessage(message_id, "user", content, datetime.now(UTC))


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_a_second_user_cannot_reach_the_first_users_session(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    try:
        await repo.create(OWNER, title="owner's conversation")
        await repo.append(OWNER, ConversationTurn("private question", "private answer"))

        assert await repo.exists(INTRUDER) is False
        assert await repo.recent(INTRUDER) == ()
        assert await repo.recent_messages(INTRUDER, limit=10) == ()
        assert await repo.messages_after(INTRUDER, after_message_id=None, limit=10) == ()
        assert await repo.rename_conversation(INTRUDER, title="hijacked") is False
        listed = await repo.list_conversations(tenant_id="ACME", user_id="user-2")
        assert listed.conversations == ()

        # Writes must not land on, or wipe, the owner's history either.
        await repo.append(INTRUDER, ConversationTurn("injected", "injected"))
        await repo.clear(INTRUDER)
        await repo.clear_messages(INTRUDER)

        assert await repo.recent(OWNER) == (ConversationTurn("private question", "private answer"),)
        owner_page = await repo.list_conversations(tenant_id="ACME", user_id="user-1")
        assert [row.title for row in owner_page.conversations] == ["owner's conversation"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_the_same_user_reaches_their_session_through_another_principal(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    try:
        await repo.create(OWNER)
        await repo.append(OWNER, ConversationTurn("question", "answer"))

        assert await repo.exists(SECOND_CREDENTIAL) is True
        assert await repo.recent(SECOND_CREDENTIAL) == (ConversationTurn("question", "answer"),)
        assert await repo.rename_conversation(SECOND_CREDENTIAL, title="renamed") is True

        await repo.append_messages(SECOND_CREDENTIAL, (_message(new_message_id(), "follow-up"),))
        assert len(await repo.recent_messages(OWNER, limit=10)) == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.graybox
async def test_appending_messages_bumps_the_session_activity_timestamp(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    repo = SqlConversationMemoryRepository(sessions)

    async def _updated_at(identity: ConversationIdentity) -> datetime:
        async with sessions() as session:
            row = await session.scalar(
                sa.select(ConversationSessionRow.updated_at).where(
                    ConversationSessionRow.session_id == identity.session_id
                )
            )
        assert row is not None
        return row

    try:
        await repo.create(OWNER)
        created = await _updated_at(OWNER)

        await repo.append(OWNER, ConversationTurn("question", "answer"))
        bumped = await _updated_at(OWNER)
        assert bumped >= created

        # A different human appending cannot move the owner's activity clock.
        async with sessions.begin() as session:
            await session.execute(
                sa.update(ConversationSessionRow)
                .where(ConversationSessionRow.session_id == OWNER.session_id)
                .values(updated_at=datetime(2020, 1, 1, tzinfo=UTC))
            )
        await repo.append_messages(INTRUDER, (_message(new_message_id(), "injected"),))
        assert await _updated_at(OWNER) == datetime(2020, 1, 1, tzinfo=UTC)
    finally:
        await engine.dispose()


@pytest.mark.whitebox
def test_migration_0024_backfills_user_ownership_and_activity(tmp_path: Path) -> None:
    config = _build_config(f"sqlite+aiosqlite:///{tmp_path}/control.db")
    command.upgrade(config, "0023")
    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    created_at = datetime(2026, 8, 10, 5, 0, tzinfo=UTC)
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO conversation_sessions "
                    "(session_id, tenant_id, principal_id, kind, created_at) "
                    "VALUES ('s-1', 'ACME', 'legacy-principal', 'chat', :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO conversation_messages (message_id, tenant_id, principal_id, "
                    "session_id, role, content, created_at, seq) VALUES "
                    "('m-1', 'ACME', 'legacy-principal', 's-1', 'user', 'q', :created_at, 1)"
                ),
                {"created_at": created_at},
            )

        command.upgrade(config, "0024")

        with sync_engine.connect() as connection:
            session_row = connection.execute(
                sa.text(
                    "SELECT user_id, title, created_at, updated_at "
                    "FROM conversation_sessions WHERE session_id = 's-1'"
                )
            ).one()
            message_user = connection.execute(
                sa.text("SELECT user_id FROM conversation_messages WHERE message_id = 'm-1'")
            ).scalar()
            indexes = {
                row[0]
                for row in connection.execute(
                    sa.text("SELECT name FROM sqlite_master WHERE type = 'index'")
                ).all()
                if row[0]
            }
        # user_id is backfilled from the credential that owned the row, and
        # updated_at from created_at, so nothing starts out un-listable.
        assert session_row.user_id == "legacy-principal"
        assert session_row.title is None
        assert session_row.updated_at == session_row.created_at
        assert message_user == "legacy-principal"
        assert "ix_conversation_messages_user_seq" in indexes
        assert "ix_conversation_messages_session_seq" in indexes
        assert "ix_conversation_sessions_user_updated" in indexes
        assert "ix_conversation_messages_identity_seq" not in indexes
    finally:
        sync_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_a_partial_answer_stays_marked_across_a_database_round_trip(
    tmp_path: Path,
) -> None:
    """An interrupted stream's text must not read back as a finished answer.

    The turn is persisted so the user does not lose what they already saw, but
    a later turn replaying it as complete would put words in the assistant's
    mouth it never finished saying.
    """

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlConversationMemoryRepository(create_session_factory(engine))
    try:
        await repo.create(OWNER)
        cut_short = ConversationMessage(
            new_message_id(),
            "assistant",
            "the stream stopped here",
            datetime.now(UTC),
            partial=True,
        )
        finished = ConversationMessage(
            new_message_id(),
            "assistant",
            "this one completed",
            datetime.now(UTC),
        )
        await repo.append_messages(OWNER, (cut_short, finished))

        stored = await repo.recent_messages(OWNER, limit=10)

        assert [message.partial for message in stored] == [True, False]
    finally:
        await engine.dispose()
