"""Per-session serialization for completions that read then write memory."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ports.conversation import ConversationIdentity, ConversationTurnLeases


class ConversationTurnBusyError(HarborConflictError):
    """The session remained busy beyond the configured acquisition deadline."""


class ConversationTurnLeaseLostError(HarborConflictError):
    """A running completion could no longer prove exclusive session ownership."""


class SessionLocks:
    """Serialize turns locally and, when supplied, through a shared lease store.

    A completion recalls the latest turns, calls the model, then appends the
    new turn. Two concurrent completions on one session (a client retry, a
    double-submit) would otherwise interleave ``recent -> append`` and both
    miss each other's turn. Holding the session lock for the whole completion
    serializes them. Locks are dropped once nobody holds or awaits them, so
    the map does not grow with the number of sessions ever seen.

    Production callers supply the conversation repository. Omitting it is an
    explicit process-local fallback for small test doubles. Shared leases are
    renewed during inference, expire after worker crashes, and cancel their
    owner if renewal fails. Idempotent response replay is a separate concern.
    """

    def __init__(
        self,
        repository: ConversationTurnLeases | None = None,
        *,
        lease_seconds: float = 120,
        acquire_timeout: float = 30,
        poll_interval: float = 0.1,
    ) -> None:
        if any(
            not math.isfinite(value) or value <= 0
            for value in (lease_seconds, acquire_timeout, poll_interval)
        ):
            raise ValueError("conversation lock durations must be positive and finite")
        self._repository = repository
        self._lease_seconds = lease_seconds
        self._acquire_timeout = acquire_timeout
        self._poll_interval = poll_interval
        self._locks: dict[ConversationIdentity, asyncio.Lock] = {}
        self._holders: Counter[ConversationIdentity] = Counter()

    @asynccontextmanager
    async def hold(self, identity: ConversationIdentity) -> AsyncIterator[None]:
        lock = self._locks.setdefault(identity, asyncio.Lock())
        self._holders[identity] += 1
        deadline = asyncio.get_running_loop().time() + self._acquire_timeout
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    await lock.acquire()
            except TimeoutError as exc:
                raise ConversationTurnBusyError("conversation has an active completion") from exc
            try:
                async with self._hold_shared(identity, deadline=deadline):
                    yield
            finally:
                lock.release()
        finally:
            self._holders[identity] -= 1
            if self._holders[identity] <= 0:
                del self._holders[identity]
                self._locks.pop(identity, None)

    @asynccontextmanager
    async def _hold_shared(
        self, identity: ConversationIdentity, *, deadline: float
    ) -> AsyncIterator[None]:
        repository = self._repository
        if repository is None:
            yield
            return
        token = uuid4().hex
        try:
            async with asyncio.timeout_at(deadline):
                while not await repository.acquire_turn_lease(
                    identity, token=token, lease_seconds=self._lease_seconds
                ):
                    await asyncio.sleep(self._poll_interval)
        except TimeoutError as exc:
            raise ConversationTurnBusyError("conversation has an active completion") from exc

        owner = asyncio.current_task()
        assert owner is not None
        lost = asyncio.Event()

        async def heartbeat() -> None:
            interval = self._lease_seconds / 3
            try:
                while True:
                    await asyncio.sleep(interval)
                    async with asyncio.timeout(interval):
                        renewed = await repository.renew_turn_lease(
                            identity, token=token, lease_seconds=self._lease_seconds
                        )
                    if not renewed:
                        raise ConversationTurnLeaseLostError("conversation lease expired")
            except Exception:
                lost.set()
                owner.cancel()

        renewal = asyncio.create_task(heartbeat(), name="conversation-turn-lease")
        try:
            yield
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            try:
                await repository.release_turn_lease(identity, token=token)
            finally:
                if lost.is_set():
                    # Consume only the cancellation sent by this heartbeat.
                    owner.uncancel()
                    raise ConversationTurnLeaseLostError("conversation turn lease was lost")

    def active(self, identity: ConversationIdentity) -> bool:
        """Whether a completion currently holds or awaits this session's lock."""

        return self._holders[identity] > 0


__all__ = ["ConversationTurnBusyError", "ConversationTurnLeaseLostError", "SessionLocks"]
