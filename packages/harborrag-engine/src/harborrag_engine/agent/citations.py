"""Trace source evidence from tool results into validated answer citations."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from unicodedata import category

from harborrag_core.ports.agent_runs import AgentEvidenceReference, AgentToolExecution

_SOURCE_RESULT_KEYS = {
    "vector_search": ("results",),
    "fetch_evidence": ("items",),
    "composed_evidence_search": ("items",),
    "get_document_context": ("chunks",),
}
_SOURCE_TOP_LEVEL_KEYS = {
    "vector_search": frozenset(
        {"ok", "error", "request_id", "lane", "results", "diagnostics", "cost"}
    ),
    "fetch_evidence": frozenset(
        {"ok", "error", "request_id", "contract_revision", "items", "completion"}
    ),
    "composed_evidence_search": frozenset(
        {
            "ok",
            "error",
            "request_id",
            "contract_revision",
            "items",
            "diagnostics",
            "cost",
            "completion",
        }
    ),
    "get_document_context": frozenset(
        {
            "ok",
            "error",
            "request_id",
            "contract_revision",
            "outcome",
            "document_id",
            "document_version_id",
            "chunks",
            "outline",
            "outline_complete",
            "next_cursor",
            "completion",
        }
    ),
}
_MARKER = re.compile(r"\[Source(?:[ :][^\]\r\n]*)?\]", re.IGNORECASE)
_MAX_CITABLE_EVIDENCE = 8
_MAX_EVIDENCE_ITEMS_SCANNED = 128
_MAX_CITATION_EXCERPT_CHARS = 800
_MAX_CHUNK_ID_CHARS = 256
_MAX_DOCUMENT_ID_CHARS = 128
_MAX_DOCUMENT_TITLE_CHARS = 256
_MAX_SECTION_PARTS = 16
_MAX_SECTION_PART_CHARS = 128
_REDACTED_STATUS_KEYS = ("availability", "status")


@dataclass(frozen=True, slots=True)
class AgentCitationAssessment:
    """Answer markers that can and cannot be traced to successful tool evidence."""

    citations: tuple[AgentEvidenceReference, ...]
    marker_count: int
    invalid_markers: tuple[str, ...]


def evidence_references(
    tool: str, result: Mapping[str, object]
) -> tuple[AgentEvidenceReference, ...]:
    """Extract canonical source chunks from a known evidence-producing tool result."""

    if result.get("ok") is not True or tool not in _SOURCE_RESULT_KEYS:
        return ()

    parent_document_id = _identifier(result.get("document_id"), _MAX_DOCUMENT_ID_CHARS)
    references: list[AgentEvidenceReference] = []
    seen: set[str] = set()
    for key in _SOURCE_RESULT_KEYS[tool]:
        items = result.get(key)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            continue
        for index, item in enumerate(items):
            if index >= _MAX_EVIDENCE_ITEMS_SCANNED:
                break
            if not isinstance(item, Mapping):
                continue
            reference = _reference(tool, item, parent_document_id=parent_document_id)
            if reference is None or reference.chunk_id in seen:
                continue
            seen.add(reference.chunk_id)
            references.append(reference)
            if len(references) >= _MAX_CITABLE_EVIDENCE:
                return tuple(references)
    return tuple(references)


def tool_result_with_citation_guide(
    tool: str,
    result: Mapping[str, object],
    references: Sequence[AgentEvidenceReference],
) -> dict[str, object]:
    """Add trusted copy-exact markers before untrusted result content reaches the model."""

    allowed_keys = _SOURCE_TOP_LEVEL_KEYS.get(tool)
    cleaned = {
        key: value
        for key, value in result.items()
        if key not in {"citation_guide", "citation_instruction"}
        and (allowed_keys is None or key in allowed_keys)
    }
    cleaned = _without_unciteable_content(tool, cleaned, references)
    if not references:
        return cleaned
    return {
        "citation_instruction": (
            "Copy the marker field exactly when citing its evidence_excerpt. "
            "Do not create a different source or citation format."
        ),
        "citation_guide": [
            {
                "marker": reference.marker,
                "evidence_excerpt": _evidence_excerpt(result, reference),
            }
            for reference in references
        ],
        **cleaned,
    }


def _without_unciteable_content(
    tool: str,
    result: Mapping[str, object],
    references: Sequence[AgentEvidenceReference],
) -> dict[str, object]:
    """Hide source text that has no marker; its identifier remains available for a follow-up."""

    identifier_key = "id" if tool == "vector_search" else "chunk_id"
    allowed = {reference.chunk_id: reference for reference in references}
    parent_document_id = _identifier(result.get("document_id"), _MAX_DOCUMENT_ID_CHARS)
    redacted = False
    cleaned = dict(result)
    for result_key in _SOURCE_RESULT_KEYS.get(tool, ()):
        items = result.get(result_key)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            if items is not None:
                cleaned[result_key] = []
                redacted = True
            continue
        retained: set[str] = set()
        visible_items: list[object] = []
        for index, item in enumerate(items):
            if index >= _MAX_EVIDENCE_ITEMS_SCANNED:
                redacted = True
                break
            if not isinstance(item, Mapping):
                visible_items.append({"citation_content_redacted": True})
                redacted = True
                continue
            visible: Mapping[object, object]
            identifier = item.get(identifier_key)
            candidate = _reference(tool, item, parent_document_id=parent_document_id)
            if (
                isinstance(identifier, str)
                and identifier in allowed
                and candidate == allowed[identifier]
                and identifier not in retained
            ):
                retained.add(identifier)
                visible = dict(item)
            else:
                visible = _redacted_source_item(item, identifier_key)
                redacted = True
            visible_items.append(visible)
        cleaned[result_key] = visible_items
    if redacted:
        cleaned["citation_content_truncated"] = True
    return cleaned


def _redacted_source_item(
    item: Mapping[object, object], identifier_key: str
) -> dict[object, object]:
    """Retain bounded routing metadata without exposing arbitrary source payloads."""

    visible: dict[object, object] = {"citation_content_redacted": True}
    identifier = _identifier(item.get(identifier_key), _MAX_CHUNK_ID_CHARS)
    if identifier is not None:
        visible[identifier_key] = identifier
    for key in _REDACTED_STATUS_KEYS:
        value = _display_text(item.get(key), 64)
        if value is not None:
            visible[key] = value
    if "error" in item:
        visible["error"] = True
    return visible


def assess_answer_citations(
    answer: str, executions: Sequence[AgentToolExecution]
) -> AgentCitationAssessment:
    """Accept only markers backed by source chunks from successful executions."""

    available: dict[str, AgentEvidenceReference] = {}
    for execution in executions:
        if not execution.ok:
            continue
        for reference in execution.evidence:
            available.setdefault(reference.marker, reference)

    citations: list[AgentEvidenceReference] = []
    invalid: list[str] = []
    cited_chunks: set[str] = set()
    markers = [match.group(0) for match in _MARKER.finditer(answer)]
    for marker in markers:
        cited_reference = available.get(marker)
        if cited_reference is None:
            invalid.append(marker)
        elif cited_reference.chunk_id not in cited_chunks:
            cited_chunks.add(cited_reference.chunk_id)
            citations.append(cited_reference)
    return AgentCitationAssessment(tuple(citations), len(markers), tuple(invalid))


def _reference(
    tool: str,
    item: Mapping[object, object],
    *,
    parent_document_id: str | None,
) -> AgentEvidenceReference | None:
    if item.get("availability", "available") != "available":
        return None
    if not _has_bounded_text(item.get("text")):
        return None
    chunk_id = _identifier(
        item.get("chunk_id" if tool != "vector_search" else "id"), _MAX_CHUNK_ID_CHARS
    )
    if chunk_id is None:
        return None

    metadata = item.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    document_id = (
        _identifier(item.get("document_id"), _MAX_DOCUMENT_ID_CHARS)
        or _identifier(metadata_map.get("document_id"), _MAX_DOCUMENT_ID_CHARS)
        or parent_document_id
    )
    if document_id is None:
        return None
    score = _score(item.get("score"))
    document_title = _display_text(
        item.get("document_title"), _MAX_DOCUMENT_TITLE_CHARS
    ) or _display_text(metadata_map.get("document_title"), _MAX_DOCUMENT_TITLE_CHARS)
    section_path = _section_path(item.get("section_path") or metadata_map.get("section_path"))
    locator = item.get("citation_locator") or metadata_map.get("citation_locator")
    location = _location(locator, item.get("ordinal"))
    return AgentEvidenceReference(
        tool,
        chunk_id,
        document_id,
        score,
        document_title,
        section_path,
        location,
        content=str(item["text"]),
    )


def _evidence_excerpt(result: Mapping[str, object], reference: AgentEvidenceReference) -> str:
    identifier_key = "id" if reference.tool == "vector_search" else "chunk_id"
    parent_document_id = _identifier(result.get("document_id"), _MAX_DOCUMENT_ID_CHARS)
    for result_key in _SOURCE_RESULT_KEYS.get(reference.tool, ()):
        items = result.get(result_key)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            continue
        for index, item in enumerate(items):
            if index >= _MAX_EVIDENCE_ITEMS_SCANNED:
                break
            if (
                not isinstance(item, Mapping)
                or item.get(identifier_key) != reference.chunk_id
                or _reference(
                    reference.tool,
                    item,
                    parent_document_id=parent_document_id,
                )
                != reference
            ):
                continue
            content = _display_text(item.get("text"), _MAX_CITATION_EXCERPT_CHARS)
            if content is not None:
                return content
    return ""


def _identifier(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or len(value) > limit + 2:
        return None
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= limit else None


def _display_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    safe = "".join(
        character for character in value[: limit + 1] if not category(character).startswith("C")
    )
    normalized = " ".join(safe.split()).strip()
    return normalized[:limit] if normalized else None


def _has_bounded_text(value: object) -> bool:
    return isinstance(value, str) and bool(value[: _MAX_CITATION_EXCERPT_CHARS + 1].strip())


def _score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _section_path(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    parts: list[str] = []
    for part in value[:_MAX_SECTION_PARTS]:
        normalized = _display_text(part, _MAX_SECTION_PART_CHARS)
        if normalized is not None:
            parts.append(normalized)
    return tuple(parts)


def _location(locator: object, ordinal: object) -> str | None:
    if isinstance(locator, Mapping):
        page = _range_location(locator, "page_start", "page_end", "page")
        if page is not None:
            return page
        line = _range_location(locator, "start_line", "end_line", "line")
        if line is not None:
            return line
    if isinstance(ordinal, int) and not isinstance(ordinal, bool) and 0 <= ordinal <= 1_000_000_000:
        return f"passage {ordinal + 1}"
    return None


def _range_location(
    locator: Mapping[object, object], start_key: str, end_key: str, label: str
) -> str | None:
    start = locator.get(start_key)
    end = locator.get(end_key)
    if (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start <= 1_000_000_000
        and 0 <= end <= 1_000_000_000
    ):
        return f"{label} {start}" if start == end else f"{label}s {start}–{end}"
    return None


__all__ = [
    "AgentCitationAssessment",
    "assess_answer_citations",
    "evidence_references",
    "tool_result_with_citation_guide",
]
