"""Public response projection for chat completions."""

from __future__ import annotations

from collections.abc import Sequence
from unicodedata import category

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import HarborChatResponse, HarborChatStreamChunk
from harborrag_core.models.cost import ModelCost


def citation_data(result: RetrievalResult) -> dict[str, object]:
    """Project one retrieval result with human-readable source provenance."""

    metadata = result.metadata
    section = _citation_section(metadata.get("section_path"))
    citation: dict[str, object] = {
        "document_id": str(result.metadata.get("document_id", "")),
        "chunk_id": result.id,
        "score": result.score,
    }
    title = _display_text(metadata.get("document_title"), 256)
    if title is not None:
        citation["document_title"] = title
    if section:
        citation["section_path"] = section
    location = _citation_location(metadata.get("citation_locator"))
    if location is not None:
        citation["location"] = location
    return citation


def citation_marker(index: int, result: RetrievalResult) -> str:
    """Return the exact readable label the model may copy for one passage."""

    metadata = result.metadata
    title = _marker_text(
        _display_text(metadata.get("document_title"), 256)
        or _display_text(metadata.get("document_id"), 128)
        or "Untitled document",
        120,
    )
    section = " > ".join(_citation_section(metadata.get("section_path")))
    location = section or _citation_location(metadata.get("citation_locator")) or "source passage"
    return f'[Source {index}: "{title}" — {_marker_text(location, 220)}]'


def _display_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    safe = "".join(
        character
        for character in value[: limit + 1]
        if not category(character).startswith("C")
    )
    normalized = " ".join(safe.split()).strip()
    return normalized[:limit] if normalized else None


def _marker_text(value: str, limit: int) -> str:
    normalized = _display_text(value, limit) or "unknown"
    return normalized.replace("[", "(").replace("]", ")").replace('"', "'")


def _citation_section(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    parts: list[str] = []
    for item in value[:16]:
        part = _display_text(item, 128)
        if part is not None:
            parts.append(part)
    return tuple(parts)


def _citation_location(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for start_key, end_key, label in (
        ("page_start", "page_end", "page"),
        ("start_line", "end_line", "line"),
    ):
        start = value.get(start_key)
        end = value.get(end_key)
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
        ):
            return f"{label} {start}" if start == end else f"{label}s {start}–{end}"
    return None


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
    cited: list[RetrievalResult] = []
    for index, result in enumerate(results, start=1):
        readable = citation_marker(index, result)
        if readable in answer:
            cited.append(result)
    return tuple(cited)


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
        "cost": ModelCost().add_call(response.estimated_cost_usd).model_dump(mode="json"),
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
        "cost": ModelCost().add_call(chunk.estimated_cost_usd).model_dump(mode="json"),
    }
