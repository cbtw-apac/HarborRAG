"""Durable request claims preventing duplicate completion work and model spend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

CompletionClaimStatus = Literal["claimed", "in_progress", "completed", "failed", "conflict"]


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
