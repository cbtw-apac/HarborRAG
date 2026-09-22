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


@pytest.mark.asyncio
async def test_released_claim_frees_the_key_without_recording_a_failure(
    sessions: SessionFactory,
) -> None:
    """A request that never reached a model must not consume its key.

    ``finish_completion(response_json=None)`` records ``failed``, and the only
    way past a failed claim is a brand-new key -- the right outcome when spend
    may have happened, the wrong one for a turn that never dispatched.
    """

    store = SqlCompletionRequestStore(sessions)
    request = {"tenant_id": "tenant", "user_id": "user", "key": "key", "request_hash": "hash"}
    assert (await store.claim_completion(**request)).status == "claimed"
    await store.release_completion(**request)
    assert (await store.claim_completion(**request)).status == "claimed"


@pytest.mark.asyncio
async def test_release_leaves_a_settled_claim_alone(sessions: SessionFactory) -> None:
    """Only an active claim is releasable; a stored answer is never thrown away."""

    store = SqlCompletionRequestStore(sessions)
    request = {"tenant_id": "tenant", "user_id": "user", "key": "key", "request_hash": "hash"}
    await store.claim_completion(**request)
    await store.finish_completion(**request, response_json='{"session_id":"s","text":"ok"}')
    await store.release_completion(**request)
    replay = await store.claim_completion(**request)
    assert replay.status == "completed"
    assert replay.result_json == '{"session_id":"s","text":"ok"}'


@pytest.mark.asyncio
async def test_abandoned_claim_is_taken_over_instead_of_wedging_the_key(
    sessions: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker killed between claim and finish must not wedge its key forever.

    ``in_progress`` has no other terminal transition, so without a staleness
    bound a client that derives its key from the message content could never
    send that message again.
    """

    import harborrag_adapters.repositories.database.control_plane.completion_requests as module

    store = SqlCompletionRequestStore(sessions)
    request = {"tenant_id": "tenant", "user_id": "user", "key": "key", "request_hash": "hash"}
    assert (await store.claim_completion(**request)).status == "claimed"
    # Still held while the holder could plausibly be alive.
    assert (await store.claim_completion(**request)).status == "in_progress"

    monkeypatch.setattr(module, "STALE_CLAIM_SECONDS", -1)
    assert (await store.claim_completion(**request)).status == "claimed"
