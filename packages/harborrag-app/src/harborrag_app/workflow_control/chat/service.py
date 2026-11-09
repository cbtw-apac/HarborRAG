"""Application service for authenticated, retrieval-grounded chat completion."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.memory.access import MemoryAccess
from harborrag_app.workflow_control.memory.extraction import (
    MemoryExtractionQueue,
)
from harborrag_app.workflow_control.memory.identity import MemoryIdentity
from harborrag_app.workflow_control.memory.locks import SessionLocks
from harborrag_app.workflow_control.memory.messages import ExchangeRecord, record_exchange
from harborrag_app.workflow_control.memory.projects import require_project
from harborrag_app.workflow_control.memory.titles import conversation_title
from harborrag_app.workflow_control.memory.usage import ModelCall, record_model_usage
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import (
    HarborConnectionError,
    HarborError,
    HarborNotFoundError,
    HarborUnavailableError,
    HarborValidationError,
)
from harborrag_core.models.chat import StreamEventType
from harborrag_core.models.errors import HarborChatError
from harborrag_core.ports.control_plane import ProjectRepositoryPort
from harborrag_core.ports.conversation import (
    ConversationHistoryRepository,
    ConversationMessage,
    new_message_id,
)
from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
from harborrag_core.ports.usage import ModelUsageRepository
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
from .errors import ChatModelUnavailableError, UnknownReferenceError
from .events import cited_event, error_event
from .options import ChatExecutionOptions
from .preparation import ChatTurnResources, PreparedTurn, RuntimeProvider, prepare_turn
from .presenters import chat_stream_chunk_data, evidence_data
from .query_scope import ScopeCheck, ScopeHistory, check_query_scope, scope_history
from .scope import ConnectionCatalog, RetrievalScope, resolve_scope
from .stream_result import stream_result
from .turn import (
    DeliveredAnswer,
    RememberedTurn,
    StreamedAnswer,
    record_turn_usage,
    remember_answer,
    remember_question,
)

if TYPE_CHECKING:
    from harborrag_core.models.chat import HarborChatResponse
    from harborrag_core.ports.config_store import ConfigProvider

logger = logging.getLogger("harborrag.app.workflow_control.chat")

MEMORY_WARNING = "conversation_memory_unavailable"
# The provider adapter's own failure, reported as an ERROR chunk rather than
# raised. Named here because it is the one error event with no exception behind
# it, and the transport still has to tell it apart from the others.
STREAM_ERROR = "ChatStreamError"
_MODEL_FAILURES = (HarborChatError, HarborConnectionError, HarborUnavailableError)


class ChatApplicationService:
    """Ground chat completions in retrieved evidence and project the result."""

    def __init__(  # noqa: PLR0913 - one keyword-only collaborator per injected port
        self,
        runtime_provider: RuntimeProvider,
        settings: RuntimeSettings,
        *,
        memory: ConversationHistoryRepository,
        projects: ProjectRepositoryPort | None = None,
        memories: MemoryRepository | None = None,
        index: MemoryIndex | None = None,
        extraction: MemoryExtractionQueue | None = None,
        locks: SessionLocks | None = None,
        usage: ModelUsageRepository | None = None,
        config: ConfigProvider | None = None,
        catalog: ConnectionCatalog | None = None,
    ) -> None:
        self._resources = ChatTurnResources(
            runtime_provider=runtime_provider,
            settings=settings,
            memory=memory,
            memories=memories,
            index=index,
            usage=usage,
            config=config,
        )
        self._projects = projects
        self._catalog = catalog
        self._extraction = extraction
        # Serializes completions per session so ``build context -> append``
        # cannot interleave across concurrent requests or SQL-backed workers.
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
    ) -> AsyncGenerator[TurnEvent, None]:
        """Run one turn, yielding the passages found, the answer text, then the completion.

        Classic checks the request's scope with a model call after retrieval and
        observes the graph per settings; instant does neither. In a session the
        exchange is stored under the completion's id before ``TurnCompleted``; a
        one-off question stores nothing. Every check that can refuse the turn runs
        before any model call.

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

    async def stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[dict[str, object], None]:
        """Yield ``{"kind": ...}`` events: one ``citations``, many ``chunk``, at
        most one ``warning`` (memory not persisted), and one terminal
        ``result`` or ``error``. A transport adapts these into SSE frames.

        ``aclosing`` is what makes an abandoned stream recoverable: when the
        client hangs up, ``GeneratorExit`` lands here, and closing the inner
        generator deterministically -- rather than waiting for the garbage
        collector -- is what lets it persist the text it already delivered,
        still holding this session's lock.
        """

        try:
            identity = _identity(tenant_id, principal_id, options)
            await self._require_scope(identity)
        except Exception as exc:  # noqa: BLE001 - service streams report scope failures in band
            yield error_event(exc, "prepare chat completion stream")
            return
        async with self._locks.hold(identity.conversation()):
            events = self._stream_locked(query, identity=identity, options=options)
            async with contextlib.aclosing(events):
                async for event in events:
                    yield event

    async def _stream_locked(
        self,
        query: str,
        *,
        identity: MemoryIdentity,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[dict[str, object], None]:
        try:
            await self._require_scope(identity)
            prepared = await prepare_turn(self._resources, query, identity, options)
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            yield error_event(exc, "prepare chat completion stream")
            return
        yield {
            "kind": "citations",
            "citations": tuple(
                evidence_data(index, result) for index, result in enumerate(prepared.results, 1)
            ),
            "session_id": options.session_id,
            "project_id": identity.project_id,
        }
        question = await remember_question(self._resources, identity, query)
        answer = StreamedAnswer()
        provider_failed = False
        try:
            async for chunk in self._resources.runtime().chat.stream(
                prepared.request,
                prompt=options.system,
            ):
                answer.observe(chunk)
                if chunk.event is StreamEventType.ERROR:
                    # The provider adapter reports a failure as an ERROR chunk
                    # and then raises. Treat the chunk as the single terminal
                    # error and stop consuming, so the exception path below
                    # never adds a second, differently shaped error frame.
                    logger.error(
                        "Chat provider stream failed for tenant=%s session=%s",
                        identity.tenant_id,
                        identity.session_id,
                    )
                    provider_failed = True
                    break
                yield {"kind": "chunk", "chunk": chat_stream_chunk_data(chunk)}
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            # Headers and the citations event are already on the wire, so a
            # failure here cannot become an HTTP error response -- it must
            # end the stream as a terminal in-band event instead. Whatever
            # text reached the caller was still paid for, so it is persisted
            # and marked partial before the error frame goes out.
            await self._finish(identity, question, prepared, answer.delivered())
            yield error_event(exc, "generate chat completion stream")
            return
        except BaseException:  # noqa: BLE001 - deadline or disconnect, then re-raised
            # ``CancelledError`` (the transport deadline firing) and
            # ``GeneratorExit`` (the client hanging up) are not errors this
            # service reports -- nothing can be yielded after either -- so the
            # delivered text is persisted before they propagate.
            #
            # That holds on the ``aclose()`` path, which is how both normally
            # arrive: the transport closes this generator and the writes below
            # run to completion. It is best-effort under a hard cancellation of
            # the surrounding task, where the first write to suspend re-raises
            # ``CancelledError`` and the turn is lost. Making that case durable
            # means shielding the writes *and* the session lock together, which
            # is a larger change than this handler.
            await self._finish(identity, question, prepared, answer.delivered())
            raise
        # Outside the ``try`` on purpose. Finishing the turn and then yielding
        # from inside it puts the yield under the same ``except BaseException``
        # that finishes the turn, so a client leaving at exactly that frame
        # persisted the delivered text twice.
        remembered = await self._finish(identity, question, prepared, answer.delivered())
        delivered = answer.delivered()
        if delivered.text and not provider_failed:
            yield cited_event(delivered.text, prepared, identity, options)
        if provider_failed:
            yield {"kind": "error", "error": STREAM_ERROR, "error_type": STREAM_ERROR}
            return
        if not answer.completed:
            yield {"kind": "error", "error": STREAM_ERROR, "error_type": STREAM_ERROR}
            return
        if not remembered.turn_persisted:
            yield {"kind": "warning", "warning": MEMORY_WARNING}
        title = await conversation_title(
            self._resources.memory,
            identity.conversation(),
            prompt=query if remembered.persisted else None,
        )
        result = stream_result(
            answer,
            prepared,
            session_id=identity.conversation().session_id,
            project_id=identity.project_id,
            memory_persisted=remembered.turn_persisted,
        )
        yield {"kind": "result", "result": {**result, "title": title}}

    async def _finish(
        self,
        identity: MemoryIdentity,
        question: ConversationMessage | None,
        prepared: PreparedTurn,
        delivered: DeliveredAnswer,
    ) -> RememberedTurn:
        """Persist the delivered answer and account for its tokens.

        Both halves are non-fatal by design: the caller has the answer either
        way, so a memory failure only costs the next turn its history and an
        accounting failure only costs the ledger one row. Each swallows its own
        ``Exception``, so no store or ledger fault reaches the caller.

        ``BaseException`` is deliberately not swallowed down there -- a write
        helper that ate ``CancelledError`` would break cancellation for every
        caller -- so this can still raise when the surrounding task is being
        cancelled hard. See the ``except BaseException`` handler in
        ``_stream_locked`` for what that costs.
        """

        answer = await remember_answer(
            self._resources,
            identity,
            delivered,
            results=prepared.results,
        )
        await record_turn_usage(self._resources, identity, delivered)
        return RememberedTurn(question, answer, empty=not delivered.text)

    async def _require_scope(self, identity: MemoryIdentity) -> None:
        """Both the session and the optional project must exist for this caller."""

        if not await self._resources.memory.exists(identity.conversation()):
            raise HarborNotFoundError("Conversation session was not found")
        await require_project(self._projects, identity.project_id, tenant_id=identity.tenant_id)


def _identity(tenant_id: str, principal_id: str, options: ChatExecutionOptions) -> MemoryIdentity:
    """The identity of a streamed turn, which needs a session.

    Raises:
        HarborValidationError: The options name no session.
    """
    if options.session_id is None:
        raise HarborValidationError("a streamed turn needs a session", {"field": "session_id"})
    return MemoryIdentity.build(
        tenant_id=tenant_id,
        principal_id=principal_id,
        session_id=options.session_id,
        user_id=options.user_id,
        project_id=options.project_ids[0] if len(options.project_ids) == 1 else None,
    )


def _access(identity: MemoryIdentity) -> MemoryAccess:
    return MemoryAccess(
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        user_id=identity.user_id,
        project_id=identity.project_id,
        session_id=identity.session_id,
    )


def _usage(options: ChatExecutionOptions, calls: Sequence[HarborChatResponse]) -> CompletionUsage:
    """What every model call of the turn cost; ``cost_usd`` is ``None`` when one is unpriced."""
    costs = [call.estimated_cost_usd for call in calls]
    return CompletionUsage(
        mode=options.mode,
        steps=1,
        prompt_tokens=sum(call.usage.prompt_tokens for call in calls),
        completion_tokens=sum(call.usage.completion_tokens for call in calls),
        reasoning_tokens=sum(call.usage.reasoning_tokens or 0 for call in calls),
        cost_usd=None if None in costs else sum(cost for cost in costs if cost is not None),
    )
