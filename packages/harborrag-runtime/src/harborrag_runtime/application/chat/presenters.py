"""Public response projection for chat completions."""

from __future__ import annotations

from collections.abc import Sequence
from unicodedata import category
from urllib.parse import urlsplit, urlunsplit

from harborrag_core.domain.retrieval import RetrievalResult


def citation_data(result: RetrievalResult) -> dict[str, object]:
    """Project one retrieval result with human-readable source provenance."""
    metadata = result.metadata
    section = _citation_section(metadata.get("section_path"))
    citation: dict[str, object] = {
        "document_id": str(result.metadata.get("document_id", "")),
        "chunk_id": result.id,
        "score": result.score,
    }
    title = display_text(metadata.get("document_title"), 256)
    if title is not None:
        citation["document_title"] = title
    if section:
        citation["section_path"] = section
    locator = metadata.get("citation_locator")
    location = _citation_location(locator) or citation_uri(locator)
    if location is not None:
        citation["location"] = location
    return citation


def citation_marker(index: int, result: RetrievalResult) -> str:
    """Return the ``[n]`` label the model cites passage ``index`` of its prompt with."""
    del result
    return f"[{index}]"


def display_text(value: object, limit: int) -> str | None:
    """Return ``value`` without control characters, whitespace collapsed, at most ``limit`` long."""
    if not isinstance(value, str):
        return None
    safe = "".join(
        character for character in value[: limit + 1] if not category(character).startswith("C")
    )
    normalized = " ".join(safe.split()).strip()
    return normalized[:limit] if normalized else None


def _citation_section(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    parts: list[str] = []
    for item in value[:16]:
        part = display_text(item, 128)
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


def citation_uri(value: object) -> str | None:
    """Expose a bounded source page when a chunk has no heading or page range."""
    if not isinstance(value, dict):
        return None
    raw = value.get("uri")
    if (
        not isinstance(raw, str)
        or len(raw) > 2048
        or any(category(character).startswith("C") for character in raw)
    ):
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    uri = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return uri if len(uri) <= 1024 else None


def cited_results(
    answer: str,
    results: Sequence[RetrievalResult],
) -> tuple[RetrievalResult, ...]:
    """Return the retrieved results the answer actually cited, in retrieval order.

    Retrieval returns its top-k whether or not any of it bears on the
    question, so reporting all of it as citations claims evidence the answer
    never used -- "Your name is Alex." came back citing five unrelated
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
