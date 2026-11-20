"""Durable duplicate suppression for paid completion requests.

A retry with the same ``Idempotency-Key`` and the same body within 24 hours gets the
stored completion back without a second model call. The key is scoped to the caller's
tenant and end user.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from harborrag_core.contracts.errors import (
    CompletionInProgressError,
    HarborConflictError,
    HarborConnectionError,
    HarborValidationError,
)
from harborrag_core.ports.completion_requests import CompletionRequestStore

from .completion import Completion, RetrievalFound, TurnCompleted, TurnEvent

logger = logging.getLogger("harborrag.runtime.application.chat.replay")


@dataclass(slots=True)
class CompletionAttempt:
    """One request's claim on its idempotency key, from claim to finish or release."""

    store: CompletionRequestStore
    tenant_id: str
    user_id: str
    key: str | None
    request_hash: str
    claimed: bool = False

    async def claim(self) -> str | None:
        """Claim the key; return the stored completion body when it already answered.

        Raises:
            HarborValidationError: The key was used with a different body (field
                ``Idempotency-Key``).
            CompletionInProgressError: The first request with the key is still running.
            HarborConflictError: The first request failed; a new key is needed.
        """
        if self.key is None:
            return None
        claim = await self.store.claim_completion(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            key=self.key,
            request_hash=self.request_hash,
        )
        if claim.status == "completed" and claim.result_json is not None:
            return claim.result_json
        if claim.status == "conflict":
            raise HarborValidationError(
                "Idempotency-Key was already used with a different request",
                {"field": "Idempotency-Key"},
            )
        if claim.status == "in_progress":
            raise CompletionInProgressError(
                "The first request with this Idempotency-Key is still being answered"
            )
        if claim.status != "claimed":
            raise HarborConflictError(
                "The request with this Idempotency-Key failed; use a new key to retry"
            )
        self.claimed = True
        return None

    async def settle(self, events: AsyncGenerator[TurnEvent, None]) -> Completion:
        """Run the turn, giving the key back on a failure before any model call and spending it after.

        The turn yields ``RetrievalFound`` after every read and check it makes before its
        first model call, so a failure until then (a store or retrieval outage, a busy
        session, a refused request, a caller that went away) leaves the key reusable. From
        then on a model may have been paid for, and a failure records the key as failed.

        Args:
            events: The turn's events, closed when this returns.

        Returns:
            The turn's completion.

        Raises:
            HarborConnectionError: The turn ended without its answer.
        """
        spent = False
        try:
            async with contextlib.aclosing(events):
                async for event in events:
                    if isinstance(event, RetrievalFound):
                        spent = True
                    elif isinstance(event, TurnCompleted):
                        return event.completion
        except BaseException:
            await (self.finish() if spent else self.release())
            raise
        await self.finish()
        raise HarborConnectionError("The chat turn ended without its answer")

    async def release(self) -> None:
        """Give the key back after a refusal that came before any model call.

        Marking such a claim failed would spend the key on work that produced
        nothing, and the only way past a failed claim is a new key.
        """
        if not self.claimed or self.key is None:
            return
        try:
            await self.store.release_completion(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                key=self.key,
                request_hash=self.request_hash,
            )
        except Exception:  # noqa: BLE001 - a stranded claim must not mask the real error
            logger.exception("Completion claim could not be released")
        finally:
            self.claimed = False

    async def finish(self, body: str | None = None, *, session_id: str | None = None) -> None:
        """Store the completion body, or record a failure when ``body`` is ``None``."""
        if not self.claimed or self.key is None:
            return
        try:
            await self.store.finish_completion(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                key=self.key,
                request_hash=self.request_hash,
                response_json=body,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001 - never discard an already paid-for result
            logger.exception("Completion replay state could not be saved")
        finally:
            self.claimed = False
