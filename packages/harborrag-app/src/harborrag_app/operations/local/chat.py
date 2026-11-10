"""In-process chat completion and session operations.

Handles:
- The completion's mode, filters and connections as chat options, refused before any model call
- ``Idempotency-Key`` replay: the key is claimed, given back on a refusal before the first
  model call, and spent after it
- The caller's conversation key, built from the verified principal, never from request fields

Does NOT set headers or wrap event frames in a response; the transport does.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from harborrag_app.api.sse import sse_frame
from harborrag_app.api.v1.chat.dispatch import chat_options
from harborrag_app.api.v1.chat.replay import CompletionAttempt
from harborrag_app.api.v1.chat.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatSessionCreateRequest,
    MessageList,
    Session,
    SessionList,
)
from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.errors import ReferenceNotFoundError
from harborrag_app.operations.port import ChatCompletion
from harborrag_app.operations.scope import Scope
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_app.workflow_control.chat.completion import (
    Completion,
    RetrievalFound,
    TurnCompleted,
)
from harborrag_app.workflow_control.memory import MemoryAccess
from harborrag_core.contracts.errors import HarborConnectionError, HarborNotFoundError

if TYPE_CHECKING:
    from harborrag_app.workflow_control.composition.service import AppService


class ChatOps:
    """The chat completion and session operations of ``LocalOperations``.

    ``LocalOperations`` sets ``_app_service``, whose chat, conversation and completion-request
    services run them.
    """

    _app_service: AppService

    async def create_chat_completion(
        self,
        scope: Scope,
        body: ChatCompletionRequest,
        *,
        idempotency_key: str | None,
        trace_id: str,
    ) -> ChatCompletion:
        """Answer one question with numbered citations, in a session or as a one-off.

        The answer is complete before this returns, so every refusal is raised here; for
        ``stream=true`` the events only frame it. The same ``idempotency_key`` and body within
        24 hours return the stored answer without a model call.

        Args:
            scope: Caller and tenant; the caller's ``user_id`` owns the session.
            body: The question, its mode, its narrowing and how to deliver the answer.
            idempotency_key: Key under which the answer is stored for replay, when set.
            trace_id: Trace id of the request, recorded with the answer.

        Returns:
            The answer as ``body``, or as ``events`` for ``stream=true``, and whether it was
            replayed.

        Raises:
            ModeUnavailableError: ``mode`` is ``advanced`` or ``auto``.
            HarborValidationError: ``filters`` cannot be applied, or ``idempotency_key`` was
                used with another body.
            CompletionInProgressError: The first request with ``idempotency_key`` is still
                being answered.
            HarborConflictError: The first request with ``idempotency_key`` failed, or its
                stored answer predates this API version.
            HarborConnectionError: The turn ended without its answer.
        """
        scope.require(OPERATIONS["create_chat_completion"].role)
        options = chat_options(body, scope.principal, trace_id=trace_id)
        attempt = CompletionAttempt.for_request(
            self._app_service,
            body,
            tenant_id=scope.tenant_id,
            principal=scope.principal,
            key=idempotency_key,
        )
        answer_json = await attempt.claim()
        replayed = answer_json is not None
        if answer_json is None:
            completion = await self._answer(scope, body, options, attempt)
            answer_json = ChatCompletionResponse.model_validate(completion.data()).model_dump_json()
            await attempt.finish(answer_json, session_id=completion.session_id)
        if body.stream:
            return ChatCompletion(
                None, _completion_frames(answer_json, replayed=replayed), replayed
            )
        return ChatCompletion(
            ChatCompletionResponse.model_validate_json(answer_json), None, replayed
        )

    async def create_chat_session(self, scope: Scope, body: ChatSessionCreateRequest) -> Session:
        """Start a session, scoped to one project of the tenant for its whole life.

        Args:
            scope: Caller and tenant; the caller's ``user_id`` owns the session.
            body: Optional title and project.

        Returns:
            The new session.

        Raises:
            ReferenceNotFoundError: ``project_id`` does not exist in the tenant.
            HarborConnectionError: The chat service did not create the session.
        """
        scope.require(OPERATIONS["create_chat_session"].role)
        try:
            result = await self._app_service.create_chat_session(
                tenant_id=scope.tenant_id,
                principal_id=scope.principal.subject,
                user_id=scope.principal.user_id,
                title=body.title,
                project_id=body.project_id,
            )
        except HarborNotFoundError as exc:
            raise ReferenceNotFoundError(str(exc), {"field": "project_id"}) from exc
        if not result.ok:
            raise HarborConnectionError("Chat service is unavailable")
        return Session.model_validate(result.data)

    async def list_chat_sessions(
        self, scope: Scope, *, cursor: str | None, limit: int, q: str | None
    ) -> SessionList:
        """List the caller's own sessions, newest activity first.

        Args:
            scope: Caller and tenant; another user's sessions are never listed.
            cursor: ``next_cursor`` of the previous page.
            limit: Page size.
            q: Title search, when set.

        Raises:
            HarborValidationError: ``cursor`` is malformed or not the caller's.
        """
        scope.require(OPERATIONS["list_chat_sessions"].role)
        result = await self._app_service.list_conversations(
            _access(scope), cursor=cursor, limit=limit, title_query=q
        )
        return SessionList.model_validate(result.data)

    async def list_conversation_messages(
        self, scope: Scope, session_id: str, *, cursor: str | None, limit: int
    ) -> MessageList:
        """List one of the caller's sessions' messages with their citations, newest first.

        Args:
            scope: Caller and tenant.
            session_id: The session to read.
            cursor: ``next_cursor`` of the previous page.
            limit: Page size.

        Raises:
            HarborNotFoundError: The session does not exist or is not the caller's.
            HarborValidationError: ``cursor`` is malformed or not from this session.
        """
        scope.require(OPERATIONS["list_conversation_messages"].role)
        result = await self._app_service.conversation_messages(
            _access(scope, session_id), cursor=cursor, limit=limit
        )
        return MessageList.model_validate(result.data)

    async def _answer(
        self,
        scope: Scope,
        body: ChatCompletionRequest,
        options: ChatExecutionOptions,
        attempt: CompletionAttempt,
    ) -> Completion:
        """Run the turn, giving the key back on a failure before any model call and spending it after.

        The turn yields ``RetrievalFound`` after every read and check it makes before its
        first model call, so a failure until then (a store or retrieval outage, a busy
        session, a refused request, a caller that went away) leaves the key reusable. From
        then on a model may have been paid for, and a failure records the key as failed.

        Raises:
            HarborConnectionError: The turn ended without its answer.
        """
        spent = False
        events = self._app_service.chat_answer(
            body.message,
            tenant_id=scope.tenant_id,
            principal_id=scope.principal.subject,
            options=options,
        )
        try:
            async with contextlib.aclosing(events):
                async for event in events:
                    if isinstance(event, RetrievalFound):
                        spent = True
                    elif isinstance(event, TurnCompleted):
                        return event.completion
        except BaseException:
            await (attempt.finish() if spent else attempt.release())
            raise
        await attempt.finish()
        raise HarborConnectionError("The chat turn ended without its answer")


def _access(scope: Scope, session_id: str | None = None) -> MemoryAccess:
    """The caller's own conversation key, built only from verified claims."""
    return MemoryAccess(
        tenant_id=scope.tenant_id,
        principal_id=scope.principal.subject,
        user_id=scope.principal.user_id,
        session_id=session_id,
    )


# TODO: stream the answer as it is generated, with the Story 5 event names (Task 7).
async def _completion_frames(answer_json: str, *, replayed: bool) -> AsyncGenerator[bytes, None]:
    """Frame a finished answer as ``response.started``, one delta, then ``response.completed``."""
    completion = json.loads(answer_json)
    yield sse_frame(
        "response.started",
        {"session_id": completion["session_id"], "mode": completion["mode"], "replayed": replayed},
    )
    yield sse_frame("response.output_text.delta", {"content": completion["answer"]})
    yield sse_frame("response.completed", completion)
