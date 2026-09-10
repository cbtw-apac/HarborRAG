"""Public response projection for chat completions."""

from __future__ import annotations

import re
from collections.abc import Sequence

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import HarborChatResponse, HarborChatStreamChunk


def citation_data(result: RetrievalResult) -> dict[str, object]:
    """Project one retrieval result as a public citation, without raw metadata."""

    return {
        "document_id": str(result.metadata.get("document_id", "")),
        "chunk_id": result.id,
        "score": result.score,
    }


# ``[Source 3]`` -- the marker ``prompt_text`` asks the model to cite with.
_CITATION_MARKER = re.compile(r"\[Source (\d+)\]")


def cited_results(
    answer: str,
    results: Sequence[RetrievalResult],
) -> tuple[RetrievalResult, ...]:
    """The retrieved results the answer actually cited, in retrieval order.

    Retrieval returns its top-k whether or not any of it bears on the
    question, so reporting all of it as citations claims evidence the answer
    never used -- "Your name is Huy." came back citing five unrelated
    documents. The model is instructed to mark what it uses, and marks
    nothing when it uses nothing, which is the only signal here that
    distinguishes the two.

    An index outside the retrieved set is dropped rather than raising: the
    marker is model output, and a hallucinated number must not become a
    citation of the wrong document.
    """

    if not results:
        return ()
    wanted = {int(match) for match in _CITATION_MARKER.findall(answer)}
    return tuple(result for index, result in enumerate(results, start=1) if index in wanted)


def chat_response_data(
    response: HarborChatResponse,
    results: Sequence[RetrievalResult] = (),
    *,
    session_id: str,
    project_id: str | None = None,
    memory_persisted: bool = True,
) -> dict[str, object]:
    """Project a provider response without leaking deployment metadata.

    ``project_id`` echoes the validated project the turn was scoped to (or
    None). ``memory_persisted`` is False when the answer was generated but could not
    be appended to conversation memory; the answer is still returned.

    ``citations`` are the sources the answer cited, not everything retrieval
    returned: retrieval hands back its top-k regardless of relevance, so
    reporting all of it claims evidence the answer never used.
    """

    return {
        "id": response.id,
        "created": response.created,
        "model": response.logical_model,
        "provider": response.provider,
        "provider_model": response.provider_model,
        "message": {
            "role": response.message.role.value,
            "content": response.text,
        },
        "finish_reason": str(response.finish_reason),
        "usage": response.usage.model_dump(mode="json"),
        "latency_ms": response.latency_ms,
        "retry_count": response.retry_count,
        "fallback_count": response.fallback_count,
        "citations": tuple(
            citation_data(result) for result in cited_results(response.text, results)
        ),
        "session_id": session_id,
        "project_id": project_id,
        "memory_persisted": memory_persisted,
    }


def chat_stream_chunk_data(chunk: HarborChatStreamChunk) -> dict[str, object]:
    """Project one stream chunk without leaking deployment/provider-error metadata."""

    return {
        "event": chunk.event.value,
        "model": chunk.logical_model,
        "provider": chunk.provider,
        "provider_model": chunk.provider_model,
        "content": chunk.text_delta,
        "reasoning": chunk.reasoning_delta,
        "finish_reason": chunk.finish_reason,
        "usage": chunk.usage.model_dump(mode="json") if chunk.usage is not None else None,
    }
