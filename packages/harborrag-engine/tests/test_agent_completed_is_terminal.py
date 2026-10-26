"""A committed COMPLETED checkpoint is final, whatever happens after it.

The memory write and event emits that follow the commit are advisory. A
cancellation landing in them -- an SSE client leaving while the answer is
written to memory -- must still propagate, but must not overwrite the answer
with a CANCELLED checkpoint that a later resume would replay.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from agent_test_helpers import Chat, Runs, Tools
from agent_test_helpers import response as _response

from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.ports.agent_runs import AgentRunStatus
from harborrag_engine.agent import AgentRunOptions, AgentService

_OPTIONS = AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1")


class BlockingMemory:
    """Conversation memory whose append blocks until the test cancels it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def recent(self, identity, *, limit=2):  # type: ignore[no-untyped-def]
        del identity, limit
        return ()

    async def recent_messages(self, identity, *, limit):  # type: ignore[no-untyped-def]
        del identity, limit
        return ()

    async def append_messages(self, identity, messages):  # type: ignore[no-untyped-def]
        del identity, messages
        self.entered.set()
        await asyncio.sleep(30)

    async def clear(self, identity):  # type: ignore[no-untyped-def]
        del identity


@pytest.mark.asyncio
async def test_a_cancellation_after_the_commit_keeps_the_run_completed() -> None:
    runs = Runs()
    memory = BlockingMemory()
    service = AgentService(Chat([_response(text="answer")]), Tools(), memory=memory, runs=runs)

    turn = asyncio.create_task(service.run([HarborChatMessage.user("question")], _OPTIONS))
    await asyncio.wait_for(memory.entered.wait(), timeout=2)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    (stored,) = runs.checkpoints.values()
    assert stored.status is AgentRunStatus.COMPLETED
    assert stored.response is not None and stored.response.text == "answer"
    assert not stored.resumable(datetime.now(UTC)), "a finished run must not be replayed"


@pytest.mark.asyncio
async def test_a_cancellation_inside_the_commit_after_it_persisted_keeps_the_run_completed() -> (
    None
):
    # The COMPLETED save has landed, but the cancel arrives before the commit
    # call has returned to the loop -- the loop must still treat it as final.
    runs = Runs()
    service = AgentService(Chat([_response(text="answer")]), Tools(), memory=None, runs=runs)
    lifecycle = service._loop.lifecycle  # noqa: SLF001 - pinning the commit window
    original = lifecycle.commit_completion
    persisted = asyncio.Event()

    async def commit_then_linger(*args, **kwargs):  # type: ignore[no-untyped-def]
        await original(*args, **kwargs)
        persisted.set()
        await asyncio.sleep(30)

    lifecycle.commit_completion = commit_then_linger  # type: ignore[method-assign]
    turn = asyncio.create_task(service.run([HarborChatMessage.user("question")], _OPTIONS))
    await asyncio.wait_for(persisted.wait(), timeout=2)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    (stored,) = runs.checkpoints.values()
    assert stored.status is AgentRunStatus.COMPLETED
    assert stored.response is not None
