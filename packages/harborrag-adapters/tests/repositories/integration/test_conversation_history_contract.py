"""SQL conversation store: the session and message fields the chat explorer contract reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
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
from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage

pytestmark = pytest.mark.integration

_COLUMNS = {
    "conversation_sessions": {"project_id"},
    "conversation_messages": {"mode", "trace_id", "feedback"},
}


def _identity(session_id: str, user_id: str = "alice") -> ConversationIdentity:
    return ConversationIdentity("ACME", "reader-1", session_id, user_id)


def _message(  # noqa: PLR0913 - one stored message field per argument
    message_id: str,
    role: str = "user",
    *,
    mode: str | None = None,
    trace_id: str | None = None,
    citations_json: str | None = None,
    partial: bool = False,
) -> ConversationMessage:
    return ConversationMessage(
        message_id=message_id,
        role=role,  # type: ignore[arg-type]
        content=f"text of {message_id}",
        created_at=datetime.now(UTC),
        citations_json=citations_json,
        partial=partial,
        mode=mode,
        trace_id=trace_id,
    )


@pytest_asyncio.fixture
async def repo(tmp_path: Path) -> AsyncIterator[SqlConversationMemoryRepository]:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield SqlConversationMemoryRepository(create_session_factory(engine))
    await engine.dispose()


def test_migration_0047_adds_and_removes_the_contract_columns(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    run_migrations(dsn)
    sync_url = f"sqlite:///{tmp_path / 'control.db'}"

    def columns(table: str) -> set[str]:
        engine = sa.create_engine(sync_url)
        try:
            return {column["name"] for column in sa.inspect(engine).get_columns(table)}
        finally:
            engine.dispose()

    for table, added in _COLUMNS.items():
        assert added <= columns(table)
    command.downgrade(_build_config(dsn), "0047")
    for table, added in _COLUMNS.items():
        assert not added & columns(table)
    run_migrations(dsn)
    for table, added in _COLUMNS.items():
        assert added <= columns(table)


@pytest.mark.asyncio
async def test_a_session_keeps_its_project_and_reads_back_as_a_summary(
    repo: SqlConversationMemoryRepository,
) -> None:
    owner = _identity("ses_1")
    await repo.create(owner, title="Go candidates", project_id="cpm")

    summary = await repo.get_conversation(owner)
    other = await repo.get_conversation(_identity("ses_1", "bob"))

    assert summary is not None
    assert (summary.session_id, summary.title, summary.project_id) == (
        "ses_1",
        "Go candidates",
        "cpm",
    )
    assert summary.message_count == 0
    assert summary.created_at == summary.updated_at
    assert other is None
    page = await repo.list_conversations(tenant_id="ACME", user_id="alice")
    assert [row.project_id for row in page.conversations] == ["cpm"]


@pytest.mark.asyncio
async def test_message_mode_trace_and_feedback_round_trip(
    repo: SqlConversationMemoryRepository,
) -> None:
    owner = _identity("ses_1")
    await repo.create(owner)
    answer = _message(
        "msg_2",
        "assistant",
        mode="classic",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        citations_json="[]",
    )
    await repo.append_messages(owner, (_message("msg_1"), answer))

    stored = await repo.recent_messages(owner, limit=10)

    assert stored[1] == answer
    assert stored[0].mode is None
    assert stored[0].trace_id is None


@pytest.mark.asyncio
async def test_history_pages_newest_first_without_partial_or_system_messages(
    repo: SqlConversationMemoryRepository,
) -> None:
    owner = _identity("ses_1")
    await repo.create(owner)
    await repo.append_messages(
        owner,
        (
            _message("msg_1"),
            _message("msg_2", "assistant"),
            _message("msg_3", "system"),
            _message("msg_4"),
            _message("msg_5", "assistant", partial=True),
            _message("msg_6"),
            _message("msg_7", "assistant"),
        ),
    )

    first = await repo.history_page(owner, before_message_id=None, limit=3)
    second = await repo.history_page(owner, before_message_id=first[-1].message_id, limit=3)
    summary = await repo.get_conversation(owner)

    assert [message.message_id for message in first] == ["msg_7", "msg_6", "msg_4"]
    assert [message.message_id for message in second] == ["msg_2", "msg_1"]
    assert summary is not None
    assert summary.message_count == 5
    with pytest.raises(ValueError, match="cursor"):
        await repo.history_page(owner, before_message_id="msg_5", limit=3)
    with pytest.raises(ValueError, match="cursor"):
        await repo.history_page(_identity("ses_1", "bob"), before_message_id="msg_4", limit=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("blank_text", ["  ", "\n", "\t\r\n \f\v"])
async def test_blank_assistant_rows_from_agent_turns_stay_out_of_history(
    repo: SqlConversationMemoryRepository, blank_text: str
) -> None:
    owner = _identity("ses_1")
    await repo.create(owner)
    blank = ConversationMessage(
        message_id="msg_2",
        role="assistant",
        content=blank_text,
        created_at=datetime.now(UTC),
    )
    await repo.append_messages(owner, (_message("msg_1"), blank, _message("msg_3", "assistant")))

    page = await repo.history_page(owner, before_message_id=None, limit=10)
    summary = await repo.get_conversation(owner)

    assert [message.message_id for message in page] == ["msg_3", "msg_1"]
    assert summary is not None
    assert summary.message_count == 2


@pytest.mark.asyncio
async def test_titles_are_searched_case_insensitively_and_literally(
    repo: SqlConversationMemoryRepository,
) -> None:
    for session_id, title in (
        ("ses_1", "Go candidates in Hanoi"),
        ("ses_2", "Rust contractors"),
        ("ses_3", "100%_literal"),
        ("ses_4", None),
    ):
        await repo.create(_identity(session_id), title=title)
    await repo.create(_identity("ses_5", "bob"), title="Hanoi for Bob")

    async def titles(query: str) -> list[str | None]:
        page = await repo.list_conversations(tenant_id="ACME", user_id="alice", title_query=query)
        return [row.title for row in page.conversations]

    assert await titles("HANOI") == ["Go candidates in Hanoi"]
    assert await titles("%") == ["100%_literal"]
    assert await titles("0%_") == ["100%_literal"]
    assert await titles("_") == ["100%_literal"]
