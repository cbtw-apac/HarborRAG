"""Chat use-case forwarding shared by application service implementations."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable

from harborrag_core.contracts.errors import (
    HarborConnectionError,
    HarborNotFoundError,
    UnknownReferenceError,
)

from ..memory import ConversationSessionService
from ..schemas import AppResponse
from .completion import TurnEvent
from .completion_request import (
    CompletionCaller,
    CompletionOutcome,
    CompletionRequest,
    ReplayKey,
    chat_options,
)
from .options import ChatExecutionOptions
from .replay import CompletionAttempt
from .service import ChatApplicationService


class ChatClientMixin:
    """Expose chat operations from a composed chat application service."""

    _chat: ChatApplicationService
    _sessions: ConversationSessionService

    async def create_chat_session(  # noqa: PLR0913 - one component of the new session per argument
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> AppResponse:
        """Create a chat session scoped to ``project_id`` for its whole life.

        ``user_id`` owns the new conversation and defaults to the principal. The transport
        must pass the same end-user identity it later puts on
        ``ChatExecutionOptions``: the session is keyed by it, so creating as
        the credential and completing as the human would make every turn look
        like an unknown session.

        Raises:
            HarborNotFoundError: ``project_id`` does not exist in ``tenant_id``.
        """
        await self._chat.validate_project(project_id, tenant_id=tenant_id)
        return await self._sessions.create(
            tenant_id=tenant_id,
            principal_id=principal_id,
            user_id=user_id,
            title=title,
            project_id=project_id,
        )

    async def start_chat_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None,
        title: str | None,
        project_id: str | None,
    ) -> AppResponse:
        """Create a chat session, a project that does not exist being a reference error.

        Raises:
            UnknownReferenceError: ``project_id`` does not exist in ``tenant_id`` (field
                ``project_id``).
            HarborConnectionError: The chat service did not create the session.
        """
        try:
            result = await self.create_chat_session(
                tenant_id=tenant_id,
                principal_id=principal_id,
                user_id=user_id,
                title=title,
                project_id=project_id,
            )
        except HarborNotFoundError as exc:
            raise UnknownReferenceError(str(exc), {"field": "project_id"}) from exc
        if not result.ok:
            raise HarborConnectionError("Chat service is unavailable")
        return result

    async def complete_chat_request(
        self,
        request: CompletionRequest,
        *,
        caller: CompletionCaller,
        replay: ReplayKey,
        validate_body: Callable[[str], object],
    ) -> CompletionOutcome:
        """Answer one completion request once, replaying the stored answer for a repeated key.

        Every refusal of the request comes before the key is claimed and before any model
        call. A repeated ``replay.key`` with the same body returns the stored completion
        without a model call.

        Args:
            request: The question and its narrowing.
            caller: Who asks, from verified claims.
            replay: The idempotency key and the hash of the request body.
            validate_body: Checks the completion JSON against the transport's response model
                and raises when it does not fit; the key is then recorded as failed instead of
                completed, and the error propagates.

        Returns:
            The completion as JSON, and whether it was replayed.

        Raises:
            ModeUnavailableError: ``request.mode`` is ``advanced`` or ``auto``.
            HarborValidationError: ``request.filters`` cannot be applied, or the key was
                used with another body.
            CompletionInProgressError: The first request with the key is still being answered.
            HarborConflictError: The first request with the key failed.
            HarborConnectionError: The turn ended without its answer.
        """
        options = chat_options(request, user_id=caller.user_id)
        attempt = CompletionAttempt(
            self._sessions,
            caller.tenant_id,
            caller.user_id or caller.principal_id,
            replay.key,
            replay.request_hash,
        )
        stored = await attempt.claim()
        if stored is not None:
            return CompletionOutcome(stored, replayed=True)
        completion = await attempt.settle(
            self._chat.answer(
                request.message,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                options=options,
            )
        )
        body = json.dumps(completion.data(), ensure_ascii=False, separators=(",", ":"))
        try:
            validate_body(body)
        except BaseException:
            await attempt.finish()
            raise
        await attempt.finish(body, session_id=completion.session_id)
        return CompletionOutcome(body, replayed=False)

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
    ) -> bool:
        """Report whether the caller owns the chat session ``session_id``."""
        return await self._sessions.exists(
            session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            user_id=user_id,
        )

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse:
        """Answer one question as the explorer completion body; see ``ChatApplicationService``.

        Raises:
            HarborError: The turn was refused or failed; see ``ChatApplicationService.answer``.
        """
        return await self._chat.complete(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )

    def chat_answer(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[TurnEvent, None]:
        """Run one turn as events; see ``ChatApplicationService.answer``."""
        return self._chat.answer(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )

    def chat_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[dict[str, object], None]:
        """Stream one completion as ``{"kind": ...}`` events."""
        return self._chat.stream(
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            options=options,
        )
