"""Stored completion requests are purged past retention and erased with their user."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane import completion_requests
from harborrag_adapters.repositories.database.control_plane.completion_requests import (
    SqlCompletionRequestStore,
)
from harborrag_adapters.repositories.database.control_plane.conversation import (
    SqlConversationMemoryRepository,
)
from harborrag_adapters.repositories.database.control_plane.mapping import utc_now
from harborrag_adapters.repositories.database.control_plane.schemas_completion_requests import (
    CompletionRequestRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.completion_requests import KEY_RETENTION_SECONDS

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]

EXPIRED = KEY_RETENTION_SECONDS + 60


async def _store(  # noqa: PLR0913 - one stored column per argument
    sessions: SessionFactory,
    tenant_id: str,
    user_id: str,
    key: str,
    *,
    status: str = "completed",
    created_ago: int = 0,
    updated_ago: int | None = None,
    session_id: str | None = None,
) -> None:
    now = utc_now()
    async with sessions.begin() as session:
        await session.execute(
            sa.insert(CompletionRequestRow).values(
                tenant_id=tenant_id,
                user_id=user_id,
                key=key,
                request_hash="hash-1",
                status=status,
                response_json='{"answer":"DEMO-2 covers rollback."}',
                session_id=session_id,
                created_at=now - timedelta(seconds=created_ago),
                updated_at=now
                - timedelta(seconds=created_ago if updated_ago is None else updated_ago),
            )
        )


async def _keys(sessions: SessionFactory) -> set[tuple[str, str, str]]:
    async with sessions() as session:
        rows = await session.execute(
            sa.select(
                CompletionRequestRow.tenant_id,
                CompletionRequestRow.user_id,
                CompletionRequestRow.key,
            )
        )
    return {(row.tenant_id, row.user_id, row.key) for row in rows}


@pytest.mark.asyncio
async def test_claim_completion_purges_the_tenants_expired_requests_only(
    sessions: SessionFactory,
) -> None:
    await _store(sessions, "tenant", "user-1", "expired", created_ago=EXPIRED)
    await _store(
        sessions, "tenant", "user-2", "expired-failed", status="failed", created_ago=EXPIRED
    )
    await _store(
        sessions,
        "tenant",
        "user-2",
        "live-takeover",
        status="in_progress",
        created_ago=EXPIRED,
        updated_ago=0,
    )
    await _store(sessions, "tenant", "user-1", "fresh")
    await _store(sessions, "other-tenant", "user-1", "expired", created_ago=EXPIRED)

    claim = await SqlCompletionRequestStore(sessions).claim_completion(
        tenant_id="tenant", user_id="user-3", key="new", request_hash="hash-1"
    )

    assert claim.status == "claimed"
    assert await _keys(sessions) == {
        ("tenant", "user-2", "live-takeover"),
        ("tenant", "user-1", "fresh"),
        ("tenant", "user-3", "new"),
        ("other-tenant", "user-1", "expired"),
    }


@pytest.mark.asyncio
async def test_claim_completion_purges_a_bounded_batch_per_claim(
    sessions: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(completion_requests, "_PURGE_BATCH", 2)
    for index in range(3):
        await _store(sessions, "tenant", "user-1", f"expired-{index}", created_ago=EXPIRED)
    store = SqlCompletionRequestStore(sessions)

    await store.claim_completion(tenant_id="tenant", user_id="user-2", key="a", request_hash="h")
    left_after_one = {key for _, _, key in await _keys(sessions) if key.startswith("expired")}
    await store.claim_completion(tenant_id="tenant", user_id="user-2", key="b", request_hash="h")
    left_after_two = {key for _, _, key in await _keys(sessions) if key.startswith("expired")}

    assert len(left_after_one) == 1
    assert left_after_two == set()


@pytest.mark.asyncio
async def test_erase_completions_deletes_every_request_of_the_user_with_or_without_a_session(
    sessions: SessionFactory,
) -> None:
    await _store(sessions, "tenant", "user-1", "in-session", session_id="ses_a")
    await _store(sessions, "tenant", "user-1", "one-off")
    await _store(sessions, "tenant", "user-1", "pending", status="in_progress")
    await _store(sessions, "tenant", "user-2", "one-off")
    await _store(sessions, "other-tenant", "user-1", "one-off")

    erased = await SqlConversationMemoryRepository(sessions).erase_completions(
        tenant_id="tenant", user_id="user-1"
    )

    assert erased == 3
    assert await _keys(sessions) == {
        ("tenant", "user-2", "one-off"),
        ("other-tenant", "user-1", "one-off"),
    }
