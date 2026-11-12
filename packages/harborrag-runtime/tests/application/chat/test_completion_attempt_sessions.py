"""A completion attempt drives the real session service, as the chat client does."""

from __future__ import annotations

import pytest

from harborrag_core.contracts.errors import CompletionInProgressError
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory
from harborrag_runtime.application.chat.replay import CompletionAttempt
from harborrag_runtime.application.memory import ConversationSessionService

pytestmark = pytest.mark.unit


def _attempt(
    sessions: ConversationSessionService, *, request_hash: str = "hash-1"
) -> CompletionAttempt:
    return CompletionAttempt(sessions, "ACME", "alice@example.com", "key-1", request_hash)


@pytest.mark.asyncio
async def test_a_finished_claim_replays_its_body_to_the_next_attempt() -> None:
    sessions = ConversationSessionService(InMemoryConversationMemory())
    first = _attempt(sessions)

    assert await first.claim() is None
    assert first.claimed
    await first.finish('{"answer": "DEMO-1 is a runbook"}')

    assert await _attempt(sessions).claim() == '{"answer": "DEMO-1 is a runbook"}'


@pytest.mark.asyncio
async def test_a_released_claim_leaves_the_key_free_for_a_retry() -> None:
    sessions = ConversationSessionService(InMemoryConversationMemory())
    first = _attempt(sessions)
    await first.claim()

    await first.release()

    assert not first.claimed
    retry = _attempt(sessions)
    assert await retry.claim() is None
    assert retry.claimed


@pytest.mark.asyncio
async def test_a_claim_in_progress_refuses_a_second_attempt_with_the_same_key() -> None:
    sessions = ConversationSessionService(InMemoryConversationMemory())
    await _attempt(sessions).claim()

    with pytest.raises(CompletionInProgressError):
        await _attempt(sessions).claim()
