from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_adapters.repositories.database.control_plane import completion_requests
from harborrag_adapters.repositories.database.control_plane.completion_requests import (
    SqlCompletionRequestStore,
)
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.mapping import utc_now
from harborrag_adapters.repositories.database.control_plane.schemas_completion_requests import (
    CompletionRequestRow,
)
from harborrag_core.ports.completion_requests import KEY_RETENTION_SECONDS

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_control_plane_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(CompletionRequestRow.__table__.create)
    yield engine
    await engine.dispose()


async def _seed_expired(engine: AsyncEngine, count: int) -> None:
    created = utc_now() - timedelta(seconds=KEY_RETENTION_SECONDS + 60)
    async with engine.begin() as connection:
        await connection.execute(
            sa.insert(CompletionRequestRow),
            [
                {
                    "tenant_id": "tenant",
                    "user_id": "user-1",
                    "key": f"expired-{index}",
                    "request_hash": "hash-1",
                    "status": "completed",
                    "response_json": '{"answer":"DEMO-2 covers rollback."}',
                    "created_at": created,
                    "updated_at": created,
                }
                for index in range(count)
            ],
        )


async def _expired_left(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        left = await connection.scalar(
            sa.select(sa.func.count()).where(CompletionRequestRow.key.like("expired-%"))
        )
    return int(left or 0)


@pytest.mark.asyncio
async def test_claim_completion_purge_bounds_the_batch_outside_the_delete(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(completion_requests, "_PURGE_BATCH", 2)
    await _seed_expired(engine, 3)
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)

    await SqlCompletionRequestStore(create_session_factory(engine)).claim_completion(
        tenant_id="tenant", user_id="user-2", key="new", request_hash="hash-1"
    )

    deletes = [sql for sql in statements if sql.lstrip().upper().startswith("DELETE")]
    assert await _expired_left(engine) == 1
    assert len(deletes) == 1
    assert "LIMIT" not in deletes[0].upper()
    assert "SELECT" not in deletes[0].upper()


@pytest.mark.asyncio
async def test_claim_completion_purge_skips_the_delete_when_nothing_expired(
    engine: AsyncEngine,
) -> None:
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)

    await SqlCompletionRequestStore(create_session_factory(engine)).claim_completion(
        tenant_id="tenant", user_id="user-2", key="new", request_hash="hash-1"
    )

    assert not [sql for sql in statements if sql.lstrip().upper().startswith("DELETE")]
