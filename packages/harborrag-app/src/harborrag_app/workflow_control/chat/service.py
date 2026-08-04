"""Application service for authenticated, retrieval-grounded chat completion."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.sdk import HarborRAG, RetrievalLane, RetrievalRequest

from .presenters import chat_response_data, chat_stream_chunk_data, citation_data

type RuntimeProvider = Callable[[], HarborRAG]

logger = logging.getLogger("harborrag.app.workflow_control.chat")


class ChatApplicationService:
    """Ground chat completions in retrieved evidence and project the result."""

    def __init__(self, runtime_provider: RuntimeProvider, settings: RuntimeSettings) -> None:
        self._runtime_provider = runtime_provider
        self._settings = settings

    async def complete(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        system: ChatPrompt | None = None,
    ) -> AppResponse:
        try:
            results = await self._retrieve(query, tenant_id=tenant_id, principal_id=principal_id)
            request = self._build_request(
                query,
                tenant_id=tenant_id,
                principal_id=principal_id,
                results=results,
            )
            response = await self._runtime_provider().chat.complete(request, prompt=system)
            return AppResponse(True, chat_response_data(response, results))
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(logger, exc, "generate chat completion")

    async def stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        system: ChatPrompt | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        """Yield ``{"kind": ...}`` events: one ``citations``, many ``chunk``, at
        most one terminal ``error``. A transport adapts these into SSE frames.
        """
        try:
            results = await self._retrieve(query, tenant_id=tenant_id, principal_id=principal_id)
            request = self._build_request(
                query,
                tenant_id=tenant_id,
                principal_id=principal_id,
                results=results,
            )
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            failure = failure_response(logger, exc, "prepare chat completion stream")
            yield {"kind": "error", "error": failure.error}
            return
        yield {
            "kind": "citations",
            "citations": tuple(citation_data(result) for result in results),
        }
        try:
            async for chunk in self._runtime_provider().chat.stream(request, prompt=system):
                yield {"kind": "chunk", "chunk": chat_stream_chunk_data(chunk)}
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            # Headers and the citations event are already on the wire, so a
            # failure here cannot become an HTTP error response -- it must
            # end the stream as a terminal in-band event instead.
            failure = failure_response(logger, exc, "generate chat completion stream")
            yield {"kind": "error", "error": failure.error}

    async def _retrieve(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> tuple[RetrievalResult, ...]:
        response = await self._runtime_provider().retrieval.search(
            RetrievalRequest(
                access=AccessContext(principal_id=principal_id, tenant_id=TenantId(tenant_id)),
                query=query,
                top_k=self._settings.chat_retrieval_top_k,
                lane=RetrievalLane.HYBRID,
                observe_graph=self._settings.chat_retrieval_graph_search,
            )
        )
        return response.results

    def _build_request(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        results: Sequence[RetrievalResult],
    ) -> HarborChatRequest:
        request = HarborChatRequest(
            messages=(HarborChatMessage.user(self._prompt_text(query, results)),),
            sensitive=True,
        )
        metadata = request.metadata.model_copy(
            update={
                "tenant_id": tenant_id,
                "user_id": principal_id,
                "retrieval_query": query,
                "document_ids": tuple(
                    str(result.metadata.get("document_id", "")) for result in results
                ),
                "chunk_ids": tuple(result.id for result in results),
                "source_citations": tuple(citation_data(result) for result in results),
            }
        )
        return request.model_copy(update={"metadata": metadata})

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
