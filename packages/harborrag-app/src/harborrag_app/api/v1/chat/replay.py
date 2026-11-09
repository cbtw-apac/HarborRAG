"""Durable duplicate suppression for paid completion requests.

A retry with the same ``Idempotency-Key`` and the same body within 24 hours gets the
stored completion back byte for byte, without a second model call. The key is scoped
to the caller's tenant and end user.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from pydantic import ValidationError

from harborrag_app.api.auth.principal import Principal
from harborrag_core.contracts.errors import HarborConflictError, HarborValidationError
from harborrag_core.ports.completion_requests import CompletionRequestStore

from .schemas import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger("harborrag.app.api.chat")


class CompletionInProgressError(HarborConflictError):
    """The first request with this key is still being answered."""

    error_code = "session_busy"


@dataclass(slots=True)
class CompletionAttempt:
    """One request's claim on its idempotency key, from claim to finish or release."""

    store: CompletionRequestStore
    tenant_id: str
    user_id: str
    key: str | None
    request_hash: str
    claimed: bool = False

    @classmethod
    def for_request(
        cls,
        store: CompletionRequestStore,
        request: ChatCompletionRequest,
        *,
        tenant_id: str,
        principal: Principal,
        key: str | None,
    ) -> CompletionAttempt:
        """The attempt of ``request``, hashed over its whole body but ``stream``.

        ``stream`` is left out because delivery does not change the paid answer: a
        streamed and a JSON retry of one question replay each other.
        """
        serialized = request.model_dump_json(exclude={"stream"})
        return cls(
            store,
            tenant_id,
            principal.user_id or principal.subject,
            key,
            hashlib.sha256(serialized.encode()).hexdigest(),
        )

    async def claim(self) -> str | None:
        """Claim the key; return the stored completion body when it already answered.

        Raises:
            HarborValidationError: The key was used with a different body (field
                ``Idempotency-Key``).
            CompletionInProgressError: The first request with the key is still running.
            HarborConflictError: The first request failed, or its stored result predates
                this API version; a new key is needed.
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
            return _replayable(claim.result_json)
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
            # The outstanding claim continues to suppress duplicate spend.
            logger.exception("Completion replay state could not be saved")
        finally:
            self.claimed = False


def _replayable(body: str) -> str:
    """Return a stored completion body unchanged once it reads as this contract's.

    Raises:
        HarborConflictError: The body was stored by a release with another contract.
    """
    try:
        ChatCompletionResponse.model_validate_json(body)
    except ValidationError:
        raise HarborConflictError(
            "The result stored for this Idempotency-Key predates this API version; use a new key"
        ) from None
    return body


__all__ = ["CompletionAttempt", "CompletionInProgressError"]
