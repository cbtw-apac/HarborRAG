"""In-process chat completion and session operations.

The caller's conversation key is built from the verified principal, never from request
fields. Mode and filter refusals, replay and the turn itself belong to the chat service;
headers and event framing belong to the transport.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from pydantic import ValidationError

from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.port import ChatCompletion
from harborrag_app.operations.principal import Principal
from harborrag_app.operations.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatSessionCreateRequest,
    Condition,
    Filter,
    MessageList,
    Session,
    SessionList,
)
from harborrag_app.operations.scope import Scope
from harborrag_app.operations.sse import sse_frame
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_memory.access import MemoryAccess
from harborrag_runtime.application.chat.completion_request import (
    CompletionCaller,
    CompletionOutcome,
    CompletionRequest,
    ReplayKey,
)
from harborrag_runtime.application.chat.request_filters import FilterCondition, FilterGroup

if TYPE_CHECKING:
    from harborrag_runtime.application.composition.service import AppService


class ChatOps:
    """The chat completion and session operations of ``LocalOperations``.

    ``LocalOperations`` sets ``_app_service``, whose chat and conversation services run them.
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
        outcome = await self._app_service.complete_chat_request(
            _completion_request(body, trace_id),
            caller=CompletionCaller(
                scope.tenant_id,
                scope.principal.subject,
                scope.principal.user_id,
                acl_subject(scope.principal),
            ),
            replay=ReplayKey(idempotency_key, body.idempotency_hash()),
            validate_body=ChatCompletionResponse.model_validate_json,
        )
        answer = _stored_answer(outcome)
        if body.stream:
            return ChatCompletion(
                None, _completion_frames(answer, replayed=outcome.replayed), outcome.replayed
            )
        return ChatCompletion(answer, None, outcome.replayed)

    async def create_chat_session(self, scope: Scope, body: ChatSessionCreateRequest) -> Session:
        """Start a session, scoped to one project of the tenant for its whole life.

        Args:
            scope: Caller and tenant; the caller's ``user_id`` owns the session.
            body: Optional title and project.

        Returns:
            The new session.

        Raises:
            UnknownReferenceError: ``project_id`` does not exist in the tenant.
            HarborConnectionError: The chat service did not create the session.
        """
        scope.require(OPERATIONS["create_chat_session"].role)
        result = await self._app_service.start_chat_session(
            tenant_id=scope.tenant_id,
            principal_id=scope.principal.subject,
            user_id=scope.principal.user_id,
            title=body.title,
            project_id=body.project_id,
        )
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


# TODO: move onto Principal once principal.py is open to this change.
def acl_subject(principal: Principal) -> str:
    """Return the subject document ACLs check: an API key's owner, else the subject.

    MCP checks a key's owner too. Sessions and ``Idempotency-Key`` replay are keyed
    by the owner as well. Rate and capacity limits still use the key ID.
    """
    return principal.user_id if principal.kind == "api_key" else principal.subject


def _access(scope: Scope, session_id: str | None = None) -> MemoryAccess:
    """The caller's own conversation key, built only from verified claims."""
    return MemoryAccess(
        tenant_id=scope.tenant_id,
        principal_id=scope.principal.subject,
        user_id=scope.principal.user_id,
        session_id=session_id,
    )


def _completion_request(body: ChatCompletionRequest, trace_id: str) -> CompletionRequest:
    """The chat service's request for a completion body."""
    return CompletionRequest(
        message=body.message,
        session_id=body.session_id,
        mode=body.mode,
        filters=_filter_group(body.filters) if body.filters is not None else None,
        project_ids=tuple(body.project_ids or ()),
        connections=tuple(body.connections or ()),
        trace_id=trace_id,
    )


def _filter_group(wire: Filter) -> FilterGroup:
    """An explorer filter as the chat service's filter group."""
    if wire.all_of is not None:
        return FilterGroup("and", tuple(_filter_item(item) for item in wire.all_of))
    return FilterGroup("or", tuple(_filter_item(item) for item in wire.any_of or ()))


def _filter_item(wire: Condition | Filter) -> FilterCondition | FilterGroup:
    if isinstance(wire, Condition):
        return FilterCondition(wire.field, wire.op, wire.value)
    return _filter_group(wire)


def _stored_answer(outcome: CompletionOutcome) -> ChatCompletionResponse:
    """The stored completion as the wire response.

    Raises:
        HarborConflictError: A replayed body was stored by a release with another contract.
    """
    try:
        return ChatCompletionResponse.model_validate_json(outcome.body)
    except ValidationError:
        if not outcome.replayed:
            raise
        raise HarborConflictError(
            "The result stored for this Idempotency-Key predates this API version; use a new key"
        ) from None


# TODO: send response.output_text.delta frames as the model generates, not one after it ends.
async def _completion_frames(
    answer: ChatCompletionResponse, *, replayed: bool
) -> AsyncGenerator[bytes, None]:
    """Frame a finished answer as ``response.started``, one delta, then ``response.completed``."""
    completion = json.loads(answer.model_dump_json())
    yield sse_frame(
        "response.started",
        {"session_id": completion["session_id"], "mode": completion["mode"], "replayed": replayed},
    )
    yield sse_frame("response.output_text.delta", {"content": completion["answer"]})
    yield sse_frame("response.completed", completion)
