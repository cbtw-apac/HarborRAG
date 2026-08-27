"""Application service for persisted conversation session resources."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_runtime.memory import (
    ConversationIdentity,
    ConversationRepository,
    new_session_id,
)

logger = logging.getLogger("harborrag.app.workflow_control.memory.sessions")


class ConversationSessionService:
    """Create authenticated session resources before a completion is requested."""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        greetings: Sequence[str],
    ) -> None:
        if not greetings:
            raise ValueError("conversation greetings must not be empty")
        self._repository = repository
        self._greetings = tuple(greetings)

    async def create(self, *, tenant_id: str, principal_id: str) -> AppResponse:
        session_id = new_session_id()
        try:
            await self._repository.create(ConversationIdentity(tenant_id, principal_id, session_id))
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            # Both session routers turn a failed envelope into 503. Without
            # this, a control-database outage escaped as an unhandled
            # exception and the transport reported a bare 500.
            return failure_response(
                logger,
                exc,
                "create conversation session session_id=%s",
                session_id,
            )
        return AppResponse(
            True,
            {
                "session_id": session_id,
                # secrets.choice, not the module-level `random` singleton:
                # nothing in the process can seed it, so two replicas cannot
                # serve a correlated greeting sequence. The greeting is
                # presentation only -- it is never written to conversation
                # memory, so the model's context starts at the first prompt.
                "greeting": secrets.choice(self._greetings),
            },
        )

    async def exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool:
        """Report session existence, distinguishing "absent" from "cannot tell".

        The callers treat ``False`` as "unknown session" and answer 404, so a
        backend outage must not be flattened into that answer -- it is raised
        as an unavailability error the transport maps to 503.
        """

        try:
            return await self._repository.exists(
                ConversationIdentity(tenant_id, principal_id, session_id)
            )
        except Exception as exc:  # noqa: BLE001 - reviewed public message only
            logger.error(
                "look up conversation session session_id=%s",
                session_id,
                exc_info=exc,
            )
            raise HarborConnectionError("Conversation session store is unavailable") from exc


__all__ = ["ConversationSessionService"]
