"""Deterministic context headers for embedded evidence chunks.

This follows the approach of Docling's ``contextualize``: the embedded text of
a chunk is its content preceded by the context a reader would need to place it
(document, heading path, caption, code location and scope, part number), and
that header is paid for out of the same token budget as the content:

* :class:`ContextHeaderBudget` reserves header tokens from the packing limits
  before refinement and packing run (Docling's ``HybridChunker`` rule), so the
  content plus its header still fits the hard limit;
* :class:`DefaultChunkContextualizer` renders the header, trimming the least
  specific context first when a budget is given.

Lines use the labelled style of route and table headers (``Document: ...``,
``Section: a > b``) so every record kind reads the same way.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any

from harborrag_core.contracts.chunking import (
    ChunkContextualizer,
    ChunkHeaderContext,
    TokenCounter,
)

from ..config import ChunkingLimits, ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit

HEADER_SEPARATOR = "\n\n"
_PATH_SEPARATOR = " > "
_LOCATION_KEYS = ("relative_path", "path", "filename")


class DefaultChunkContextualizer:
    """Render ``Document / Section / Caption / File / Scope / Part`` header lines."""

    format_version: str = "1"

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def header(self, context: ChunkHeaderContext, *, maximum_tokens: int | None = None) -> str:
        """Return the fullest header that fits ``maximum_tokens`` (or the full one)."""

        for candidate in self._variants(context):
            text = self._lines(candidate)
            if maximum_tokens is None or self._token_counter.count(text) <= maximum_tokens:
                return text
        return ""

    def render(self, header: str, content: str) -> str:
        return f"{header}{HEADER_SEPARATOR}{content}" if header else content

    @staticmethod
    def _lines(context: ChunkHeaderContext) -> str:
        lines: list[str] = []
        if context.document_title and context.document_title.strip():
            lines.append(f"Document: {context.document_title.strip()}")
        if context.structural_path:
            lines.append(f"Section: {_PATH_SEPARATOR.join(context.structural_path)}")
        if context.caption and context.caption.strip():
            lines.append(f"Caption: {context.caption.strip()}")
        location = (context.code_location or "").strip()
        if location and context.language:
            lines.append(f"File: {location} ({context.language})")
        elif location:
            lines.append(f"File: {location}")
        elif context.language:
            lines.append(f"Language: {context.language}")
        if context.scope:
            lines.append(f"Scope: {_PATH_SEPARATOR.join(context.scope)}")
        if context.part_index is not None and context.part_count is not None:
            lines.append(f"Part: {context.part_index}/{context.part_count}")
        return "\n".join(lines)

    @staticmethod
    def _variants(context: ChunkHeaderContext) -> Iterator[ChunkHeaderContext]:
        """Yield progressively smaller contexts, least specific context dropped first.

        Order: outer heading segments, then outer scope lines, then the caption,
        then the document title. The nearest heading, the innermost scope line,
        the file, and the part marker go last because they identify the chunk.
        """

        yield context
        current = context
        while len(current.structural_path) > 1:
            current = replace(current, structural_path=current.structural_path[1:])
            yield current
        while len(current.scope) > 1:
            current = replace(current, scope=current.scope[1:])
            yield current
        if current.caption:
            current = replace(current, caption=None)
            yield current
        if current.document_title:
            current = replace(current, document_title=None)
            yield current
        if current.structural_path:
            current = replace(current, structural_path=())
            yield current
        if current.scope:
            current = replace(current, scope=())
            yield current


def header_context(
    request: ChunkingRequest,
    *,
    role: str,
    structural_path: tuple[str, ...],
    metadata: Mapping[str, Any],
) -> ChunkHeaderContext:
    """Collect header facts for one unit or candidate."""

    is_code = role == "code"
    scope = metadata.get("code_scope")
    if not (isinstance(scope, str) and scope.strip()):
        symbol = metadata.get("symbol")
        scope = symbol if isinstance(symbol, str) else None
    part_index = metadata.get("part_index")
    part_count = metadata.get("part_count")
    numbered = (
        isinstance(part_index, int)
        and isinstance(part_count, int)
        and part_count > 1
        and 1 <= part_index <= part_count
    )
    return ChunkHeaderContext(
        document_title=request.document.title.strip() or None,
        structural_path=tuple(part for part in structural_path if part.strip()),
        caption=_text(metadata.get("caption")),
        code_location=_location(request, metadata) if is_code else None,
        language=_text(metadata.get("language")) if is_code else None,
        scope=tuple(line for line in str(scope).splitlines() if line.strip())
        if isinstance(scope, str)
        else (),
        part_index=part_index if numbered else None,
        part_count=part_count if numbered else None,
    )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _location(request: ChunkingRequest, metadata: Mapping[str, Any]) -> str | None:
    for source in (metadata, request.document.provenance.extra):
        for key in _LOCATION_KEYS:
            value = _text(source.get(key))
            if value:
                return value
    return None


class ContextHeaderBudget:
    """Reserve header tokens out of the packing limits for one document."""

    # Room for a "Part: n/m" line that only exists once a unit is split.
    _PART_ALLOWANCE = "\nPart: 999/999"

    def __init__(self, contextualizer: ChunkContextualizer, token_counter: TokenCounter) -> None:
        self._contextualizer = contextualizer
        self._token_counter = token_counter

    def reserve(
        self,
        request: ChunkingRequest,
        units: tuple[ChunkUnit, ...],
        profile: ChunkingProfile,
    ) -> int:
        """Return the largest header cost of any unit, capped at a third of the limit."""

        cap = profile.maximum_tokens // 3
        separator = self._token_counter.count(HEADER_SEPARATOR)
        largest = 0
        for unit in units:
            context = header_context(
                request,
                role=unit.role,
                structural_path=unit.structural_path,
                metadata=unit.metadata,
            )
            header = self._contextualizer.header(context)
            if not header:
                continue
            if unit.token_count > profile.maximum_tokens:
                header += self._PART_ALLOWANCE
            largest = max(largest, self._token_counter.count(header) + separator)
            if largest >= cap:
                return cap
        return largest


def budgeted_profile(profile: ChunkingProfile, reserve: int) -> ChunkingProfile:
    """Return ``profile`` with every limit lowered so ``reserve`` tokens stay free."""

    if reserve < 1:
        return profile
    limits = profile.limits
    maximum = max(profile.maximum_tokens - reserve, 1)
    soft = min(profile.soft_maximum_tokens, maximum)
    target = min(profile.target_tokens, soft)
    minimum = min(profile.minimum_tokens, target)
    overlap = min(limits.overlap_tokens, max(target - 1, 0))
    return ChunkingProfile(
        name=profile.name,
        strategy=profile.strategy,
        limits=ChunkingLimits(
            minimum_tokens=minimum,
            target_tokens=target,
            maximum_tokens=maximum,
            overlap_tokens=overlap,
            soft_maximum_tokens=soft,
        ),
        merge_small_peers=profile.merge_small_peers,
        preserve_sections=profile.preserve_sections,
        repeat_table_headers=profile.repeat_table_headers,
    )
