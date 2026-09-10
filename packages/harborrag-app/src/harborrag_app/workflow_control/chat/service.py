"""Application service for authenticated, retrieval-grounded chat completion."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.memory.extraction import (
    MemoryExtractionQueue,
    submit_exchange,
)
from harborrag_app.workflow_control.memory.identity import MemoryIdentity
from harborrag_app.workflow_control.memory.locks import SessionLocks
from harborrag_app.workflow_control.memory.projects import require_project
from harborrag_app.workflow_control.memory.usage import ModelCall
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import (
    HarborNoIndexedContentError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.models.chat import StreamEventType
from harborrag_core.ports.control_plane import ProjectRepositoryPort
from harborrag_core.ports.conversation import ConversationHistoryRepository, ConversationMessage
from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_runtime.config.settings import RuntimeSettings

from .events import cited_event, error_event
from .options import ChatExecutionOptions
from .preparation import ChatTurnResources, PreparedTurn, RuntimeProvider, prepare_turn
from .presenters import chat_response_data, chat_stream_chunk_data, citation_data
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

logger = logging.getLogger("harborrag.app.workflow_control.chat")

MEMORY_WARNING = "conversation_memory_unavailable"
# The provider adapter's own failure, reported as an ERROR chunk rather than
# raised. Named here because it is the one error event with no exception behind
# it, and the transport still has to tell it apart from the others.
STREAM_ERROR = "ChatStreamError"


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
    ) -> None:
        self._resources = ChatTurnResources(
            runtime_provider=runtime_provider,
            settings=settings,
            memory=memory,
            memories=memories,
            index=index,
            usage=usage,
        )
        self._projects = projects
        self._extraction = extraction
        # Serializes completions per session so ``build context -> append``
        # cannot interleave across concurrent requests (in-process only).
        self._locks = locks or SessionLocks()

    async def validate_model(self, model: str | None, *, tenant_id: str) -> None:
        """Reject a model name this tenant may not use, before the turn starts.

        The runtime owns the rule because only it can see both a tenant's own
        catalog and the process-wide one. Callers ask up front so a rejected
        name is a validation failure rather than a provider error mid-stream.
        """

        await self._resources.runtime().chat.validate_model(model, tenant_id=tenant_id)

    async def validate_project(self, project_id: str | None, *, tenant_id: str) -> None:
        """Reject a project this tenant does not have, before the turn starts.

        ``_require_scope`` makes the same check once a turn is under way, which
        is enough for the JSON path: it raises before anything is written and
        the transport turns it into a ``404``. A stream cannot do that -- by
        the time the generator runs, the status line is already sent -- so the
        transport asks here first and gets one status code instead of a ``200``
        whose body says the service is unavailable.
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
        identity = _identity(tenant_id, principal_id, options)
        await self._require_scope(identity)
        try:
            async with self._locks.hold(identity.conversation()):
                prepared, question, response = await self._answer(query, identity, options)
                remembered = await self._finish(
                    identity,
                    question,
                    prepared,
                    DeliveredAnswer(response.text, ModelCall.from_response(response)),
                )
                submit_exchange(self._extraction, identity, query, remembered.persisted)
            return AppResponse(
                True,
                chat_response_data(
                    response,
                    prepared.results,
                    session_id=options.session_id,
                    project_id=identity.project_id,
                    memory_persisted=remembered.turn_persisted,
                ),
            )
        except (HarborValidationError, HarborNoIndexedContentError):
            # Conditions the transport must report as themselves. A rejected
            # value is a 422 and an empty index is a 409; folding either into
            # the generic envelope below would tell the caller the chat
            # service is down when it is healthy and the fix is theirs.
            raise
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(logger, exc, "generate chat completion")

    async def _answer(
        self,
        query: str,
        identity: MemoryIdentity,
        options: ChatExecutionOptions,
    ) -> tuple[PreparedTurn, ConversationMessage | None, HarborChatResponse]:
        """Prepare, commit to the question, then call the model -- in that order.

        The memory context is assembled before the question is persisted so the
        replayed window never contains the question being answered; the
        question is persisted before the model is called so a provider failure
        still leaves a record that the turn happened.
        """

        prepared = await prepare_turn(self._resources, query, identity, options)
        question = await remember_question(self._resources, identity, query)
        response = await self._resources.runtime().chat.complete(
            prepared.request,
            prompt=options.system,
        )
        return prepared, question, response

    async def stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[dict[str, object], None]:
        """Yield ``{"kind": ...}`` events: one ``citations``, many ``chunk``, at
        most one ``warning`` (memory not persisted) and at most one terminal
        ``error``. A transport adapts these into SSE frames.

        ``aclosing`` is what makes an abandoned stream recoverable: when the
        client hangs up, ``GeneratorExit`` lands here, and closing the inner
        generator deterministically -- rather than waiting for the garbage
        collector -- is what lets it persist the text it already delivered,
        still holding this session's lock.
        """

        identity = _identity(tenant_id, principal_id, options)
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
            "citations": tuple(citation_data(result) for result in prepared.results),
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
        if not remembered.turn_persisted:
            yield {"kind": "warning", "warning": MEMORY_WARNING}
            return
        submit_exchange(self._extraction, identity, query, remembered.persisted)

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

        if not await self._resources.memory.exists(identity.conversation(), kind="chat"):
            raise HarborNotFoundError("Conversation session was not found")
        await require_project(self._projects, identity.project_id, tenant_id=identity.tenant_id)


def _identity(tenant_id: str, principal_id: str, options: ChatExecutionOptions) -> MemoryIdentity:
    return MemoryIdentity.build(
        tenant_id=tenant_id,
        principal_id=principal_id,
        session_id=options.session_id,
        user_id=options.user_id,
        project_id=options.project_id,
    )
