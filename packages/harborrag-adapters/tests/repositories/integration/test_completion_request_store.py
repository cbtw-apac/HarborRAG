"""Concurrent request claims, response replay, and isolation of request keys."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_adapters.repositories.database.control_plane.completion_requests import (
    SqlCompletionRequestStore,
)
from harborrag_adapters.repositories.database.control_plane.conversation import (
    SqlConversationMemoryRepository,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.conversation import ConversationIdentity

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]


@pytest.mark.asyncio
async def test_claim_is_atomic_and_completed_response_is_replayed(sessions: SessionFactory) -> None:
    store = SqlCompletionRequestStore(sessions)
    request = {"tenant_id": "tenant", "user_id": "user", "key": "key", "request_hash": "hash"}
    claims = await asyncio.gather(*(store.claim_completion(**request) for _ in range(8)))
    assert [claim.status for claim in claims].count("claimed") == 1
    assert [claim.status for claim in claims].count("in_progress") == 7
    await store.finish_completion(**request, response_json='{"session_id":"session","text":"ok"}')
    claim = await store.claim_completion(**request)
    assert claim.status == "completed"
    assert claim.result_json == '{"session_id":"session","text":"ok"}'
    await store.finish_completion(**request, response_json=None)
    assert (await store.claim_completion(**request)).status == "completed"


@pytest.mark.asyncio
async def test_hash_conflicts_and_failures_never_start_duplicate_work(
    sessions: SessionFactory,
) -> None:
    store = SqlCompletionRequestStore(sessions)
    request = {"tenant_id": "tenant", "user_id": "user", "key": "key", "request_hash": "hash"}
    assert (await store.claim_completion(**request)).status == "claimed"
    conflict = {**request, "request_hash": "other"}
    assert (await store.claim_completion(**conflict)).status == "conflict"
    await store.finish_completion(**conflict, response_json="foreign result")
    assert (await store.claim_completion(**request)).status == "in_progress"
    await store.finish_completion(**request, response_json=None)
    assert (await store.claim_completion(**request)).status == "failed"
    for scope in ({"user_id": "other-user"}, {"tenant_id": "other-tenant"}):
        assert (await store.claim_completion(**{**request, **scope})).status == "claimed"


@pytest.mark.asyncio
async def test_erasure_removes_replay_text_without_allowing_a_new_claim(
    sessions: SessionFactory,
) -> None:
    repo = SqlConversationMemoryRepository(sessions)
    identity = ConversationIdentity("tenant", "principal", "session", "user")
    request = {"tenant_id": "tenant", "user_id": "user", "key": "key", "request_hash": "hash"}
    await repo.create(identity)
    await repo.claim_completion(**request)
    await repo.finish_completion(**request, session_id="session", response_json="private answer")
    assert (await repo.claim_completion(**request)).status == "completed"
    assert await repo.delete(identity)
    claim = await repo.claim_completion(**request)
    assert claim.status == "failed"
    assert claim.result_json is None
    # An in-flight response arriving after the delete cannot restore the text.
    pending = {**request, "key": "pending"}
    await repo.claim_completion(**pending)
    await repo.finish_completion(**pending, session_id="session", response_json="late answer")
    claim = await repo.claim_completion(**pending)
    assert claim.status == "failed"
    assert claim.result_json is None
