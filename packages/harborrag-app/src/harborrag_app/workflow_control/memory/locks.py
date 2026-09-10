"""Per-session serialization for completions that read then write memory."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from harborrag_runtime.memory import ConversationIdentity


class SessionLocks:
    """In-process ``asyncio.Lock`` per conversation identity, created on demand.

    A completion recalls the latest turns, calls the model, then appends the
    new turn. Two concurrent completions on one session (a client retry, a
    double-submit) would otherwise interleave ``recent -> append`` and both
    miss each other's turn. Holding the session lock for the whole completion
    serializes them. Locks are dropped once nobody holds or awaits them, so
    the map does not grow with the number of sessions ever seen.

    This is process-local: it does not deduplicate replays across replicas.
    Full ``Idempotency-Key`` replay needs a shared response store.
    """

    def __init__(self) -> None:
        self._locks: dict[ConversationIdentity, asyncio.Lock] = {}
        self._holders: Counter[ConversationIdentity] = Counter()

    @asynccontextmanager
    async def hold(self, identity: ConversationIdentity) -> AsyncIterator[None]:
        lock = self._locks.setdefault(identity, asyncio.Lock())
        self._holders[identity] += 1
        try:
            async with lock:
                yield
        finally:
            self._holders[identity] -= 1
            if self._holders[identity] <= 0:
                del self._holders[identity]
                self._locks.pop(identity, None)

    def active(self, identity: ConversationIdentity) -> bool:
        """Whether a completion currently holds or awaits this session's lock."""

        return self._holders[identity] > 0


__all__ = ["SessionLocks"]
