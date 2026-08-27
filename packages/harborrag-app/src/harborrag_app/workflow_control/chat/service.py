"""Application service for authenticated, retrieval-grounded chat completion."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    StreamEventType,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import (
    ConversationIdentity,
    ConversationRepository,
    ConversationTurn,
)
from harborrag_runtime.sdk import HarborRAG, RetrievalLane, RetrievalRequest

from .options import ChatExecutionOptions
from .presenters import chat_response_data, chat_stream_chunk_data, citation_data

type RuntimeProvider = Callable[[], HarborRAG]

logger = logging.getLogger("harborrag.app.workflow_control.chat")


@dataclass(frozen=True, slots=True)
class _ChatIdentity:
    tenant_id: str
    session_id: str


class ChatApplicationService:
    """Ground chat completions in retrieved evidence and project the result."""

    def __init__(
        self,
        runtime_provider: RuntimeProvider,
        settings: RuntimeSettings,
        *,
        memory: ConversationRepository,
    ) -> None:
        self._runtime_provider = runtime_provider
        self._settings = settings
        self._memory = memory

    async def complete(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse:
        identity = ConversationIdentity(tenant_id, principal_id, options.session_id)
        try:
            # Inside the try so a memory-store outage becomes a 503 envelope
            # rather than an unhandled 500, but re-raised below so an absent
            # session still reaches the transport as 404.
            await self._require_session(identity)
            chat_identity = _ChatIdentity(tenant_id, options.session_id)
            history = await self._history(identity)
            results = await self._retrieve(
                query,
                tenant_id=tenant_id,
                principal_id=principal_id,
                graph_search=options.graph_search,
            )
            request = self._build_request(
                query,
                identity=chat_identity,
                results=results,
                history=history,
            )
            response = await self._runtime_provider().chat.complete(
                request,
                prompt=options.system,
            )
            await self._remember(identity, query, response.text)
            return AppResponse(
                True,
                chat_response_data(
                    response,
                    results,
                    session_id=options.session_id,
                ),
            )
        except HarborNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(
                logger,
                exc,
                "generate chat completion session_id=%s",
                options.session_id,
            )

    async def stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]:
        """Yield ``{"kind": ...}`` events: one ``citations``, many ``chunk``, at
        most one terminal ``error``. A transport adapts these into SSE frames.
        """
        identity = ConversationIdentity(tenant_id, principal_id, options.session_id)
        try:
            await self._require_session(identity)
            chat_identity = _ChatIdentity(tenant_id, options.session_id)
            history = await self._history(identity)
            results = await self._retrieve(
                query,
                tenant_id=tenant_id,
                principal_id=principal_id,
                graph_search=options.graph_search,
            )
            request = self._build_request(
                query,
                identity=chat_identity,
                results=results,
                history=history,
            )
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            failure = failure_response(
                logger,
                exc,
                "prepare chat completion stream session_id=%s",
                options.session_id,
            )
            yield {"kind": "error", "error": failure.error}
            return
        yield {
            "kind": "citations",
            "citations": tuple(citation_data(result) for result in results),
            "session_id": options.session_id,
        }
        text_parts: list[str] = []
        completed = False
        try:
            async for chunk in self._runtime_provider().chat.stream(
                request,
                prompt=options.system,
            ):
                if chunk.text_delta:
                    text_parts.append(chunk.text_delta)
                if chunk.event is StreamEventType.COMPLETED:
                    completed = True
                yield {"kind": "chunk", "chunk": chat_stream_chunk_data(chunk)}
            if completed:
                await self._remember(
                    identity,
                    query,
                    "".join(text_parts),
                )
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            # Headers and the citations event are already on the wire, so a
            # failure here cannot become an HTTP error response -- it must
            # end the stream as a terminal in-band event instead.
            failure = failure_response(
                logger,
                exc,
                "generate chat completion stream session_id=%s",
                options.session_id,
            )
            yield {"kind": "error", "error": failure.error}

    async def _retrieve(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        graph_search: bool | None,
    ) -> tuple[RetrievalResult, ...]:
        response = await self._runtime_provider().retrieval.search(
            RetrievalRequest(
                access=AccessContext(principal_id=principal_id, tenant_id=TenantId(tenant_id)),
                query=query,
                top_k=self._settings.chat_retrieval_top_k,
                lane=RetrievalLane.HYBRID,
                observe_graph=(
                    self._settings.chat_retrieval_graph_search
                    if graph_search is None
                    else graph_search
                ),
            )
        )
        return response.results

    def _build_request(
        self,
        query: str,
        *,
        identity: _ChatIdentity,
        results: Sequence[RetrievalResult],
        history: Sequence[HarborChatMessage] = (),
    ) -> HarborChatRequest:
        request = HarborChatRequest(
            messages=(*history, HarborChatMessage.user(self._prompt_text(query, results))),
            sensitive=True,
        )
        metadata = request.metadata.model_copy(
            update={
                "tenant_id": identity.tenant_id,
                "conversation_id": identity.session_id,
                "retrieval_query": query,
                "document_ids": tuple(
                    str(result.metadata.get("document_id", "")) for result in results
                ),
                "chunk_ids": tuple(result.id for result in results),
                "source_citations": tuple(citation_data(result) for result in results),
            }
        )
        return request.model_copy(update={"metadata": metadata})

    async def _history(
        self,
        identity: ConversationIdentity,
    ) -> tuple[HarborChatMessage, ...]:
        turns = await self._memory.recent(identity, limit=self._settings.chat_history_turns)
        return _turn_messages(turns)

    async def _remember(
        self,
        identity: ConversationIdentity,
        query: str,
        response: str,
    ) -> None:
        await self._memory.append(identity, ConversationTurn(query, response))

    async def _require_session(self, identity: ConversationIdentity) -> None:
        if not await self._memory.exists(identity):
            raise HarborNotFoundError("Conversation session was not found")

    @staticmethod
    def _prompt_text(query: str, results: Sequence[RetrievalResult]) -> str:
        """Fold retrieval into one explicitly delimited turn.

        The chat provider adapter renders every ``HarborChatMessage`` by role
        only (`build_litellm_messages`) -- it does not special-case
        ``context_kind``. Sending each chunk as its own
        ``HarborChatMessage.retrieved_context(...)`` message would therefore
        reach the model as an indistinguishable extra user turn. Folding
        everything into one labeled block keeps context and question
        unambiguous regardless of provider.
        """

        if not results:
            return query
        context = "\n\n".join(
            f"[Source {index}] (document_id={result.metadata.get('document_id', 'unknown')})\n"
            f"{result.text}"
            for index, result in enumerate(results, start=1)
        )
        return (
            "Use the retrieved context below to answer the question. "
            "If it is insufficient, say so instead of guessing.\n\n"
            f"Retrieved context:\n{context}\n\n"
            f"Question: {query}"
        )


def _turn_messages(turns: Sequence[ConversationTurn]) -> tuple[HarborChatMessage, ...]:
    messages: list[HarborChatMessage] = []
    for turn in turns:
        messages.extend(
            (
                HarborChatMessage.user(turn.user_content),
                HarborChatMessage.assistant(turn.assistant_content),
            )
        )
    return tuple(messages)
