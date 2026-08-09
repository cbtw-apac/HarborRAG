"""Integration coverage for SQL-backed conversation memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.control_plane.conversation import (
    SqlConversationMemoryRepository,
)
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_core.ports.conversation import ConversationIdentity, ConversationTurn

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
    identity = ConversationIdentity("ACME", "principal-1", "session-1")
    other = ConversationIdentity("ACME", "principal-1", "session-2")
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
