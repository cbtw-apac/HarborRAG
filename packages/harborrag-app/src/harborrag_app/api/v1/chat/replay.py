"""Durable duplicate suppression for paid completion requests."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from harborrag_app.api.auth.principal import Principal
from harborrag_core.contracts.errors import HarborConflictError

from .completion_dependency import CompletionService
from .schemas import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger("harborrag.app.api.chat")


@dataclass(slots=True)
class CompletionAttempt:
    service: CompletionService
    tenant_id: str
    user_id: str
    key: str | None
    request_hash: str
    claimed: bool = False

    @classmethod
    def for_request(
        cls, service: CompletionService, request: ChatCompletionRequest, principal: Principal
    ) -> CompletionAttempt:
        # Delivery format does not alter the paid operation. A retry may ask
        # for JSON after its stream disconnected and receive the saved result.
        #
        # ``session_id`` is excluded because the server assigns it and hands it
        # back in the first stream frame: a retry that echoes the session it was
        # just told about describes the same paid operation as the attempt that
        # omitted it, and must replay rather than read as a different request.
        serialized = request.model_dump_json(exclude={"stream", "idempotency_key", "session_id"})
        return cls(
            service,
            request.tenant,
            principal.user_id or principal.subject,
            request.idempotency_key,
            hashlib.sha256(serialized.encode()).hexdigest(),
        )

    async def claim(self) -> ChatCompletionResponse | None:
        if self.key is None:
            return None
        claim = await self.service.claim_completion(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            key=self.key,
            request_hash=self.request_hash,
        )
        if claim.status == "completed" and claim.result_json is not None:
            return ChatCompletionResponse.model_validate_json(claim.result_json)
        if claim.status != "claimed":
            messages = {
                "conflict": "Idempotency key was already used with a different request",
                "in_progress": "This completion is already running or its outcome is uncertain",
                "failed": "This completion failed; use a new idempotency key to retry",
            }
            raise HarborConflictError(messages.get(claim.status, "Completion cannot be replayed"))
        self.claimed = True
        return None

    async def release(self) -> None:
        """Give the key back after a failure that never reached a model.

        Marking such a claim ``failed`` would spend the caller's key on work
        that never cost anything, and the only way past a failed claim is a
        brand-new key. Releasing keeps the duplicate-suppression window while
        the request was in flight and leaves the key reusable afterwards.
        """

        if not self.claimed or self.key is None:
            return
        try:
            await self.service.release_completion(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                key=self.key,
                request_hash=self.request_hash,
            )
        except Exception:  # noqa: BLE001 - a stranded claim must not mask the real error
            logger.exception("Completion claim could not be released")
        finally:
            self.claimed = False

    async def finish(self, result: ChatCompletionResponse | None = None) -> None:
        if not self.claimed or self.key is None:
            return
        try:
            await self.service.finish_completion(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                key=self.key,
                request_hash=self.request_hash,
                response_json=result.model_dump_json() if result is not None else None,
                session_id=result.session_id if result is not None else None,
            )
        except Exception:  # noqa: BLE001 - never discard an already paid-for result
            # The outstanding claim continues to suppress duplicate spend.
            logger.exception("Completion replay state could not be saved")
        finally:
            self.claimed = False
