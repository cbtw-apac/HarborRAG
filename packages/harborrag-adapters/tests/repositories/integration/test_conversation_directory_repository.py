"""Integration coverage for the SQL conversation directory (list + rename)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.conversation import (
    SqlConversationMemoryRepository,
)
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    ConversationSessionRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.conversation import (
    MAX_CONVERSATION_TITLE_LENGTH,
    ConversationIdentity,
    ConversationMessage,
    ConversationTurn,
    encode_conversation_cursor,
)

pytestmark = pytest.mark.integration

_BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _identity(session_id: str, user_id: str = "user-1") -> ConversationIdentity:
    return ConversationIdentity("ACME", "shared-principal", session_id, user_id)


def _message(message_id: str) -> ConversationMessage:
    return ConversationMessage(message_id, "user", "q", datetime.now(UTC))


async def _seed(repo: SqlConversationMemoryRepository, sessions: SessionFactory) -> None:
    """Three chat sessions and one agent session, with fixed activity stamps.

    ``updated_at`` is stamped explicitly rather than left to the append clock
    so ordering and paging assertions cannot tie or flake.
    """

    plan = (
        ("chat-a", "chat", 1, 1),
        ("chat-b", "chat", 2, 3),
        ("chat-c", "chat", 3, 0),
        ("agent-a", "agent", 4, 2),
    )
    for session_id, kind, offset, messages in plan:
        identity = _identity(session_id)
        await repo.create(identity, kind=kind, title=f"title {session_id}")  # type: ignore[arg-type]
        for index in range(messages):
            await repo.append_messages(identity, (_message(f"{session_id}-{index}"),))
        async with sessions.begin() as session:
            await session.execute(
                sa.update(ConversationSessionRow)
                .where(ConversationSessionRow.session_id == session_id)
                .values(updated_at=_BASE + timedelta(minutes=offset))
            )


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_conversations_list_newest_first_with_message_counts(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    repo = SqlConversationMemoryRepository(sessions)
    try:
        await _seed(repo, sessions)

        page = await repo.list_conversations(tenant_id="ACME", user_id="user-1")
        assert [row.session_id for row in page.conversations] == [
            "agent-a",
            "chat-c",
            "chat-b",
            "chat-a",
        ]
        assert [row.message_count for row in page.conversations] == [2, 0, 3, 1]
        assert [row.title for row in page.conversations][0] == "title agent-a"
        assert page.next_cursor is None

        chats = await repo.list_conversations(tenant_id="ACME", user_id="user-1", kind="chat")
        assert [row.session_id for row in chats.conversations] == ["chat-c", "chat-b", "chat-a"]
        agents = await repo.list_conversations(tenant_id="ACME", user_id="user-1", kind="agent")
        assert [row.kind for row in agents.conversations] == ["agent"]
        # Another tenant with the same user id sees nothing.
        other = await repo.list_conversations(tenant_id="OTHER", user_id="user-1")
        assert other.conversations == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_conversation_paging_walks_the_whole_listing_once(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    repo = SqlConversationMemoryRepository(sessions)
    try:
        await _seed(repo, sessions)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(4):
            page = await repo.list_conversations(
                tenant_id="ACME", user_id="user-1", limit=2, cursor=cursor
            )
            seen.extend(row.session_id for row in page.conversations)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == ["agent-a", "chat-c", "chat-b", "chat-a"]
        assert cursor is None

        # Limits are clamped to 1..100, never rejected.
        clamped_low = await repo.list_conversations(tenant_id="ACME", user_id="user-1", limit=0)
        assert len(clamped_low.conversations) == 1
        clamped_high = await repo.list_conversations(
            tenant_id="ACME", user_id="user-1", limit=10_000
        )
        assert len(clamped_high.conversations) == 4
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_a_malformed_or_foreign_cursor_is_rejected(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    repo = SqlConversationMemoryRepository(sessions)
    try:
        await _seed(repo, sessions)
        await repo.create(_identity("stranger", "user-2"))
        page = await repo.list_conversations(tenant_id="ACME", user_id="user-2", limit=1)
        foreign_cursor = page.conversations[0].session_id

        # Same error type and message as the messages_after cursor path.
        with pytest.raises(ValueError, match="unknown conversation cursor"):
            await repo.list_conversations(tenant_id="ACME", user_id="user-1", cursor="!!not-b64!!")
        with pytest.raises(ValueError, match="unknown conversation cursor"):
            await repo.list_conversations(
                tenant_id="ACME",
                user_id="user-1",
                cursor=encode_conversation_cursor(foreign_cursor),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_rename_trims_bounds_and_refuses_another_users_conversation(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    repo = SqlConversationMemoryRepository(sessions)
    identity = _identity("chat-a")
    try:
        await repo.create(identity)
        await repo.append(identity, ConversationTurn("q", "a"))

        assert await repo.rename_conversation(identity, title="   Renamed   ") is True
        page = await repo.list_conversations(tenant_id="ACME", user_id="user-1")
        assert page.conversations[0].title == "Renamed"

        assert await repo.rename_conversation(identity, title="t" * 500) is True
        page = await repo.list_conversations(tenant_id="ACME", user_id="user-1")
        title = page.conversations[0].title
        assert title is not None
        assert len(title) == MAX_CONVERSATION_TITLE_LENGTH

        assert await repo.rename_conversation(identity, title="   ") is True
        page = await repo.list_conversations(tenant_id="ACME", user_id="user-1")
        assert page.conversations[0].title is None

        assert await repo.rename_conversation(_identity("chat-a", "user-2"), title="x") is False
        assert await repo.rename_conversation(_identity("missing"), title="x") is False
    finally:
        await engine.dispose()
