"""Application service for authenticated, retrieval-grounded chat completion."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from harborrag_core.contracts.errors import (
    ChatModelUnavailableError,
    HarborConnectionError,
    HarborError,
    HarborNotFoundError,
    HarborUnavailableError,
    HarborValidationError,
    UnknownReferenceError,
)
from harborrag_core.domain.identity import MemoryIdentity
from harborrag_core.models.errors import HarborChatError
from harborrag_core.ports.control_plane import ProjectRepositoryPort
from harborrag_core.ports.conversation import (
    ConversationHistoryRepository,
    new_message_id,
)
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_engine.chat.scope import ConnectionCatalog, RetrievalScope, resolve_scope
from harborrag_memory.access import MemoryAccess
from harborrag_memory.locks import SessionLocks
from harborrag_memory.messages import ExchangeRecord, record_exchange
from harborrag_memory.projects import require_project
from harborrag_memory.titles import conversation_title
from harborrag_memory.usage import ModelCall, record_model_usage
from harborrag_runtime.application.errors import failure_response
from harborrag_runtime.application.schemas import AppResponse
from harborrag_runtime.config.settings import RuntimeSettings

from .citations import CitationNumbering
from .completion import (
    AnswerDelta,
    Completion,
    CompletionUsage,
    RetrievalFound,
    TurnCompleted,
    TurnEvent,
)
from .options import ChatExecutionOptions
from .preparation import ChatTurnResources, PreparedTurn, RuntimeProvider, prepare_turn
from .query_scope import ScopeCheck, ScopeHistory, check_query_scope, scope_history

if TYPE_CHECKING:
    from harborrag_core.models.chat import HarborChatResponse
    from harborrag_core.ports.config_store import ConfigProvider

logger = logging.getLogger("harborrag.runtime.application.chat.service")

_MODEL_FAILURES = (HarborChatError, HarborConnectionError, HarborUnavailableError)


class ChatApplicationService:
    """Ground chat completions in retrieved evidence and project the result.

    Completions are serialized per session, so building the context and appending the
    exchange cannot interleave across concurrent requests or SQL-backed workers.

    Args:
        runtime_provider: Returns the runtime that retrieves and calls the chat model.
        settings: Process settings holding the chat retrieval policy.
        memory: Conversation history store.
        projects: Project lookup; ``None`` skips project checks.
        locks: Per-session locks; built over ``memory`` when omitted.
        usage: Model usage ledger, when the deployment keeps one.
        config: Runtime settings provider for per-project retrieval overrides.
        catalog: Connection catalog that resolves a request's scope.
    """

    def __init__(  # noqa: PLR0913 - one keyword-only collaborator per injected port
        self,
        runtime_provider: RuntimeProvider,
        settings: RuntimeSettings,
        *,
        memory: ConversationHistoryRepository,
        projects: ProjectRepositoryPort | None = None,
        locks: SessionLocks | None = None,
        usage: ModelUsageRepository | None = None,
        config: ConfigProvider | None = None,
        catalog: ConnectionCatalog | None = None,
    ) -> None:
        self._resources = ChatTurnResources(
            runtime_provider=runtime_provider,
            settings=settings,
            memory=memory,
            usage=usage,
            config=config,
        )
        self._projects = projects
        self._catalog = catalog
        self._locks = locks or SessionLocks(memory)

    async def validate_model(self, model: str | None, *, tenant_id: str) -> None:
        """Reject a model name this tenant may not use, before the turn starts.

        The runtime owns the rule because only it can see both a tenant's own
        catalog and the process-wide one. Callers ask up front so a rejected
        name is a validation failure rather than a provider error mid-stream.
        """
        await self._resources.runtime().chat.validate_model(model, tenant_id=tenant_id)

    async def validate_project(self, project_id: str | None, *, tenant_id: str) -> None:
        """Reject a project this tenant does not have.

        Raises:
            HarborNotFoundError: ``project_id`` does not exist in ``tenant_id``.
        """
        await require_project(self._projects, project_id, tenant_id=tenant_id)

    async def complete(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse:
        """Answer one question and return the explorer completion body.

        Raises:
            HarborError: As ``answer`` raises; any other failure is answered as an
                unavailable chat service.
        """
        events = self.answer(query, tenant_id=tenant_id, principal_id=principal_id, options=options)
        try:
            async with contextlib.aclosing(events):
                async for event in events:
                    if isinstance(event, TurnCompleted):
                        return AppResponse(True, event.completion.data())
        except HarborError:
            raise
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(logger, exc, "generate chat completion")
        raise RuntimeError("a chat turn ended without its completion")

    async def answer(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
        validate: Callable[[Completion], object] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        """Run one turn, yielding the passages found, the answer text, then the completion.

        Classic checks the request's scope with a model call after retrieval and
        observes the graph per settings; instant does neither. In a session the
        exchange is stored under the completion's id before ``TurnCompleted``; a
        one-off question stores nothing. Every check that can refuse the turn runs
        before any model call. ``validate`` sees the completion before it is stored;
        when it raises, nothing is stored and its error propagates.

        Raises:
            HarborNotFoundError: The session does not exist for this caller.
            HarborValidationError: ``project_ids`` in a session, or a request the
                retrieval or prompt budget refuses.
            UnknownReferenceError: A project or connection does not exist.
            ConnectionOutOfScopeError: A connection sits outside the scope's projects.
            HarborNoIndexedContentError: Nothing is indexed in the scope.
            ConversationTurnBusyError: Another turn is answering in the session.
            ChatModelUnavailableError: The chat model did not answer, or answered nothing.
            HarborConnectionError: The answer could not be stored in the session.
        """
        trace_id = options.trace_id or uuid4().hex
        options = replace(options, trace_id=trace_id)
        identity, scope = await self._admit(tenant_id, principal_id, options)
        async with self._turn(identity):
            prepared = await prepare_turn(
                self._resources, query, identity, options, filters=scope.filters
            )
            history = await self._scope_history(identity, options)
            yield RetrievalFound(prepared.results)
            check = await self._check_scope(query, identity, options, history)
            refusal = check.refusal if check is not None else None
            response = (
                await self._generate(prepared, identity, options) if refusal is None else None
            )
            numbering = CitationNumbering(() if response is None else prepared.results)
            text = numbering.feed(response.text if response is not None else refusal or "")
            text += numbering.finish()
            if not text.strip():
                raise ChatModelUnavailableError("The chat model returned no answer")
            yield AnswerDelta(text)
            calls = [check.response] if check is not None else []
            if response is not None:
                calls.append(response)
            completion = Completion(
                completion_id=new_message_id(),
                session_id=identity.session_id,
                trace_id=trace_id,
                mode=options.mode,
                answer=text,
                citations=numbering.citations(),
                usage=_usage(options, calls),
            )
            if validate is not None:
                validate(completion)
            answer_tokens = response.usage.completion_tokens if response is not None else None
            await self._remember(query, identity, completion, answer_tokens)
            yield TurnCompleted(completion)

    async def _admit(
        self, tenant_id: str, principal_id: str, options: ChatExecutionOptions
    ) -> tuple[MemoryIdentity, RetrievalScope]:
        """Find the session and the scope it reads, refusing before any model call."""
        await self.validate_model(options.model, tenant_id=tenant_id)
        if options.session_id is None:
            projects = tuple(dict.fromkeys(options.project_ids))
            for project_id in projects:
                try:
                    await require_project(self._projects, project_id, tenant_id=tenant_id)
                except HarborNotFoundError as exc:
                    raise UnknownReferenceError(str(exc), {"field": "project_ids"}) from exc
        else:
            projects = await self._session_projects(tenant_id, principal_id, options)
        scope = await resolve_scope(
            self._catalog,
            tenant_id=tenant_id,
            projects=projects,
            connections=options.connections,
            filters=options.filters,
        )
        identity = MemoryIdentity.build(
            tenant_id=tenant_id,
            principal_id=principal_id,
            session_id=options.session_id,
            user_id=options.user_id,
            project_id=scope.project_id,
        )
        return identity, scope

    async def _session_projects(
        self, tenant_id: str, principal_id: str, options: ChatExecutionOptions
    ) -> tuple[str, ...]:
        """Return the project the session was created for, refusing ``project_ids`` in a session."""
        if options.project_ids:
            raise HarborValidationError(
                "project_ids are for one-off questions; a session reads its own project",
                {"field": "project_ids"},
            )
        assert options.session_id is not None
        owner = MemoryIdentity.build(
            tenant_id=tenant_id,
            principal_id=principal_id,
            session_id=options.session_id,
            user_id=options.user_id,
        ).conversation()
        session = await self._resources.memory.get_conversation(owner)
        if session is None:
            raise HarborNotFoundError("Conversation session was not found")
        return (session.project_id,) if session.project_id else ()

    @asynccontextmanager
    async def _turn(self, identity: MemoryIdentity) -> AsyncIterator[None]:
        """Hold the session for the turn, refusing at once when another turn holds it."""
        if identity.session_id is None:
            yield
            return
        async with self._locks.hold(identity.conversation(), wait=False):
            yield

    async def _scope_history(
        self, identity: MemoryIdentity, options: ChatExecutionOptions
    ) -> ScopeHistory | None:
        """Classic only: the history the scope check reads, read before any model call."""
        if options.mode != "classic":
            return None
        return await scope_history(self._resources, _access(identity))

    async def _check_scope(
        self,
        query: str,
        identity: MemoryIdentity,
        options: ChatExecutionOptions,
        history: ScopeHistory | None,
    ) -> ScopeCheck | None:
        """Classic only: the scope check's call and, out of scope, the refusal to answer with."""
        if history is None:
            return None
        try:
            return await check_query_scope(
                self._resources,
                query,
                _access(identity),
                history=history,
                model=options.model,
                trace_id=options.trace_id,
            )
        except _MODEL_FAILURES as exc:
            raise ChatModelUnavailableError("The chat model is unavailable") from exc

    async def _generate(
        self, prepared: PreparedTurn, identity: MemoryIdentity, options: ChatExecutionOptions
    ) -> HarborChatResponse:
        """Call the answer model and account for the call, whatever it answered."""
        try:
            response = await self._resources.runtime().chat.complete(
                prepared.request, prompt=options.system
            )
        except _MODEL_FAILURES as exc:
            raise ChatModelUnavailableError("The chat model is unavailable") from exc
        await record_model_usage(self._resources.usage, identity, ModelCall.from_response(response))
        return response

    async def _remember(
        self,
        query: str,
        identity: MemoryIdentity,
        completion: Completion,
        answer_tokens: int | None,
    ) -> None:
        """Store a session's exchange under the completion's id, with the answer's own tokens."""
        if identity.session_id is None:
            return
        owner = identity.conversation()
        try:
            await record_exchange(
                self._resources.memory,
                owner,
                ExchangeRecord(
                    question=query,
                    answer=completion.answer,
                    mode=completion.mode,
                    trace_id=completion.trace_id,
                    citations=completion.citations,
                    completion_tokens=answer_tokens,
                    answer_id=completion.completion_id,
                ),
            )
        except HarborError:
            raise
        except Exception as exc:
            raise HarborConnectionError(
                "The answer could not be stored in the session's history"
            ) from exc
        await conversation_title(self._resources.memory, owner, prompt=query)


def _access(identity: MemoryIdentity) -> MemoryAccess:
    """Return the memory access key of ``identity``."""
    return MemoryAccess(
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        user_id=identity.user_id,
        project_id=identity.project_id,
        session_id=identity.session_id,
    )


def _usage(options: ChatExecutionOptions, calls: Sequence[HarborChatResponse]) -> CompletionUsage:
    """Total the tokens and cost of the turn's model calls; ``cost_usd`` is ``None`` when one is unpriced."""
    costs = [call.estimated_cost_usd for call in calls]
    return CompletionUsage(
        mode=options.mode,
        steps=1,
        prompt_tokens=sum(call.usage.prompt_tokens for call in calls),
        completion_tokens=sum(call.usage.completion_tokens for call in calls),
        reasoning_tokens=sum(call.usage.reasoning_tokens or 0 for call in calls),
        cost_usd=None if None in costs else sum(cost for cost in costs if cost is not None),
    )
