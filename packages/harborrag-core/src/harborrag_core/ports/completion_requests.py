"""Durable request claims preventing duplicate completion work and model spend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

CompletionClaimStatus = Literal["claimed", "in_progress", "completed", "failed", "conflict"]

# How long an ``in_progress`` claim is honoured before a retry may take it over.
# Nothing else ends that state, so a worker killed mid-request would otherwise
# wedge its key forever. Deliberately well beyond the longest a request can
# legitimately stay in flight (``api_stream_timeout_seconds`` defaults to 600s),
# so a claim this old means the holder is gone rather than slow.
STALE_CLAIM_SECONDS = 900


@dataclass(frozen=True, slots=True)
class CompletionClaim:
    """The result of claiming one key within an authenticated user's scope."""

    status: CompletionClaimStatus
    result_json: str | None = None


class CompletionRequestStore(Protocol):
    """Claim once, then retain a terminal response or failure for duplicate requests.

    Failed and interrupted claims are never automatically retried: a model may
    already have charged for a request whose result could not be persisted.
    A caller intentionally retrying that work must choose a new key.

    ``release_completion`` is the one exception, and it does not weaken that
    rule: it applies only where the request never reached a model at all, so
    no spend can have occurred and the key was consumed for nothing.
    """

    async def claim_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim: ...

    async def finish_completion(  # noqa: PLR0913 - explicit scoped idempotency contract
        self,
        *,
        tenant_id: str,
        user_id: str,
        key: str,
        request_hash: str,
        response_json: str | None,
        session_id: str | None = None,
    ) -> None:
        """Finish the matching active claim; none records an unreplayable failure."""
        ...

    async def release_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> None:
        """Drop an active claim whose request never reached a model.

        For failures that occur before any dispatch -- a busy conversation
        turn, an unavailable session store, a caller that disconnected while
        queueing -- nothing was charged, so the key must become reusable
        instead of recording a failure the caller can never retry past.
        """
        ...
