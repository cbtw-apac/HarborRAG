from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from harborrag_core.chunking import ChunkRecord, RecordKind, thaw_metadata
from harborrag_core.contracts.chunking import TokenCounter

from ..config import ChunkingProfile
from ..identity import content_fingerprint
from ..schemas import ChunkingRequest, ChunkValidationResult

type ChunkRecordValidator = Callable[[ChunkRecord, list[str], str], None]


@dataclass(slots=True)
class _SourceOrderState:
    element_order: dict[str, int]
    previous_index: int = -1
    locations_by_element: dict[str, tuple[int, int, int]] = field(default_factory=dict)


class ChunkValidator:
    """Validate limits, identities, provenance, metadata, and source order."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def validate(
        self,
        records: tuple[ChunkRecord, ...],
        request: ChunkingRequest,
        profile: ChunkingProfile,
        *,
        source_validator: ChunkRecordValidator | None = None,
        require_route: bool = False,
    ) -> ChunkValidationResult:
        """Validate canonical records against identity and provenance invariants."""

        errors: list[str] = []
        warnings: list[str] = []
        self._validate_route(records, require_route=require_route, errors=errors)
        self._validate_unique_identities(records, errors)
        source_order = _SourceOrderState(
            element_order={
                element.id: index for index, element in enumerate(request.document.content)
            }
        )

        for expected_ordinal, record in enumerate(records):
            label = f"chunk[{expected_ordinal}]"
            self._validate_content(record, profile, errors, warnings, label)
            self._validate_provenance(record, request, errors, label)
            self._validate_metadata(record, errors, label)
            self._validate_source_order(record, source_order, errors, label)
            self._validate_neighbors(records, expected_ordinal, errors, label)
            if source_validator is not None:
                source_validator(record, errors, label)

        return ChunkValidationResult(
            valid=not errors,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _validate_route(
        records: tuple[ChunkRecord, ...],
        *,
        require_route: bool,
        errors: list[str],
    ) -> None:
        if require_route and not records:
            errors.append("a document must contain at least one required route chunk")
        elif require_route and records[0].record_kind != RecordKind.ROUTE:
            errors.append("the first chunk must be the required document route")

    @staticmethod
    def _validate_unique_identities(
        records: tuple[ChunkRecord, ...],
        errors: list[str],
    ) -> None:
        logical_ids = {str(record.logical_chunk_id) for record in records}
        revision_ids = {str(record.chunk_id) for record in records}
        if len(logical_ids) != len(records):
            errors.append("logical_chunk_id values must be unique")
        if len(revision_ids) != len(records):
            errors.append("chunk_id values must be unique")

    def _validate_content(
        self,
        record: ChunkRecord,
        profile: ChunkingProfile,
        errors: list[str],
        warnings: list[str],
        label: str,
    ) -> None:
        if not record.content or not record.content.strip():
            errors.append(f"{label} content is blank")
        exact_count = self._token_counter.count(record.content)
        if record.token_count != exact_count:
            errors.append(f"{label} token_count is not exact")
        if exact_count < 1:
            errors.append(f"{label} token_count must be positive")
        maximum_tokens = 512 if record.record_kind == RecordKind.ROUTE else profile.maximum_tokens
        if exact_count > maximum_tokens:
            errors.append(f"{label} exceeds maximum_tokens")
        if record.record_kind != RecordKind.ROUTE and exact_count < profile.minimum_tokens:
            warnings.append(f"{label} is below preferred minimum_tokens")
        if content_fingerprint(record.content) != record.content_hash:
            errors.append(f"{label} content_hash does not match")

    @staticmethod
    def _validate_provenance(
        record: ChunkRecord,
        request: ChunkingRequest,
        errors: list[str],
        label: str,
    ) -> None:
        expected = (
            (str(record.tenant_id), request.tenant_id, "tenant"),
            (str(record.document_id), request.document.id, "document"),
            (
                str(record.document_version_id),
                request.document_version_id,
                "document version",
            ),
        )
        for actual, required, name in expected:
            if actual != required:
                errors.append(f"{label} {name} provenance does not match request")

    @staticmethod
    def _validate_metadata(
        record: ChunkRecord,
        errors: list[str],
        label: str,
    ) -> None:
        try:
            json.dumps(
                thaw_metadata(record.metadata),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        except (TypeError, ValueError):
            errors.append(f"{label} metadata is not JSON serializable")

    @classmethod
    def _validate_source_order(
        cls,
        record: ChunkRecord,
        state: _SourceOrderState,
        errors: list[str],
        label: str,
    ) -> None:
        span = record.citation_locator
        element_ids = span.source_element_ids
        if not element_ids:
            errors.append(f"{label} has no parser source element IDs")
            return
        source_indices = [
            state.element_order[element_id]
            for element_id in element_ids
            if element_id in state.element_order
        ]
        if len(source_indices) != len(element_ids):
            errors.append(f"{label} references unknown source element IDs")
            return
        current_index = min(source_indices)
        if current_index < state.previous_index:
            errors.append(f"{label} source element order moved backward")
        state.previous_index = current_index
        cls._validate_locations(record, state.locations_by_element, errors, label)

    @classmethod
    def _validate_neighbors(
        cls,
        records: tuple[ChunkRecord, ...],
        ordinal: int,
        errors: list[str],
        label: str,
    ) -> None:
        record = records[ordinal]
        if record.ordinal != ordinal:
            errors.append(f"{label} ordinal is not contiguous")
        previous = str(records[ordinal - 1].logical_chunk_id) if ordinal else None
        next_ = str(records[ordinal + 1].logical_chunk_id) if ordinal + 1 < len(records) else None
        if cls._optional_id(record.hierarchy.previous_chunk_id) != previous:
            errors.append(f"{label} previous_chunk_id is inconsistent")
        if cls._optional_id(record.hierarchy.next_chunk_id) != next_:
            errors.append(f"{label} next_chunk_id is inconsistent")

    @staticmethod
    def _validate_locations(
        record: ChunkRecord,
        previous: dict[str, tuple[int, int, int]],
        errors: list[str],
        label: str,
    ) -> None:
        span = record.citation_locator
        location = (
            span.page_start if span.page_start is not None else -1,
            span.start_line if span.start_line is not None else -1,
            span.start_offset if span.start_offset is not None else -1,
        )
        for element_id in span.source_element_ids:
            earlier = previous.get(element_id)
            if earlier is not None and location < earlier:
                errors.append(f"{label} source location moved backward")
                break
            previous[element_id] = location

    @staticmethod
    def _optional_id(value: object | None) -> str | None:
        return str(value) if value is not None else None
