from __future__ import annotations

import json

from harborrag_core.contracts.chunking import TokenCounter
from harborrag_core.schemas.documents import ChunkRecord, thaw_metadata

from .config import ChunkingProfile
from .identity import content_fingerprint
from .schemas import ChunkingRequest, ChunkValidationResult


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
        strategy_name: str,
    ) -> ChunkValidationResult:
        """Validate canonical records against identity and provenance invariants."""

        errors: list[str] = []
        warnings: list[str] = []
        logical_ids = {str(record.logical_chunk_id) for record in records}
        revision_ids = {str(record.chunk_revision_id) for record in records}
        if len(logical_ids) != len(records):
            errors.append("logical_chunk_id values must be unique")
        if len(revision_ids) != len(records):
            errors.append("chunk_revision_id values must be unique")

        element_order = {
            element.id: index for index, element in enumerate(request.document.content)
        }
        previous_source_index = -1
        previous_location_by_element: dict[str, tuple[int, int, int]] = {}

        for expected_ordinal, record in enumerate(records):
            label = f"chunk[{expected_ordinal}]"
            source_span = record.source_span
            source_element_ids = source_span.source_element_ids if source_span is not None else ()
            if not record.content or not record.content.strip():
                errors.append(f"{label} content is blank")
            if record.ordinal != expected_ordinal:
                errors.append(f"{label} ordinal is not contiguous")
            exact_count = self._token_counter.count(record.content)
            if record.token_count != exact_count:
                errors.append(f"{label} token_count is not exact")
            if exact_count < 1:
                errors.append(f"{label} token_count must be positive")
            if exact_count > profile.maximum_tokens:
                errors.append(f"{label} exceeds maximum_tokens")
            if exact_count < profile.minimum_tokens:
                warnings.append(f"{label} is below preferred minimum_tokens")
            if content_fingerprint(record.content) != record.content_hash:
                errors.append(f"{label} content_hash does not match")
            if str(record.tenant_id) != request.tenant_id:
                errors.append(f"{label} tenant provenance does not match request")
            if str(record.document_id) != request.document.id:
                errors.append(f"{label} document provenance does not match request")
            if record.artifact_id != request.artifact_id:
                errors.append(f"{label} artifact provenance does not match request")
            if record.artifact_revision_id != request.artifact_revision_id:
                errors.append(f"{label} artifact revision does not match request")
            if not source_element_ids:
                errors.append(f"{label} has no parser source element IDs")

            try:
                json.dumps(
                    thaw_metadata(record.metadata),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                )
            except (TypeError, ValueError):
                errors.append(f"{label} metadata is not JSON serializable")

            source_indices = [
                element_order[element_id]
                for element_id in source_element_ids
                if element_id in element_order
            ]
            if len(source_indices) != len(source_element_ids):
                errors.append(f"{label} references unknown source element IDs")
            elif source_indices:
                current_source_index = min(source_indices)
                if current_source_index < previous_source_index:
                    errors.append(f"{label} source element order moved backward")
                previous_source_index = current_source_index
                self._validate_locations(
                    record,
                    previous_location_by_element,
                    errors,
                    label,
                )

            expected_previous = (
                str(records[expected_ordinal - 1].logical_chunk_id)
                if expected_ordinal > 0
                else None
            )
            expected_next = (
                str(records[expected_ordinal + 1].logical_chunk_id)
                if expected_ordinal + 1 < len(records)
                else None
            )
            if self._optional_id(record.context.previous_chunk_id) != expected_previous:
                errors.append(f"{label} previous_chunk_id is inconsistent")
            if self._optional_id(record.context.next_chunk_id) != expected_next:
                errors.append(f"{label} next_chunk_id is inconsistent")

            self._validate_source_specific(
                record,
                strategy_name,
                errors,
                label,
            )

        return ChunkValidationResult(
            valid=not errors,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _validate_locations(
        record: ChunkRecord,
        previous: dict[str, tuple[int, int, int]],
        errors: list[str],
        label: str,
    ) -> None:
        span = record.source_span
        if span is None:
            return
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
    def _validate_source_specific(
        record: ChunkRecord,
        strategy_name: str,
        errors: list[str],
        label: str,
    ) -> None:
        metadata = record.metadata
        if strategy_name == "jira":
            issue_key = metadata.get("issue_key")
            if not isinstance(issue_key, str) or not issue_key.strip():
                errors.append(f"{label} Jira chunk requires issue_key")
            if record.role == "jira.comment":
                comment_id = metadata.get("comment_id")
                if not isinstance(comment_id, (str, int)) or not str(comment_id).strip():
                    errors.append(f"{label} Jira comment requires comment_id")
        elif strategy_name == "confluence":
            page_id = metadata.get("page_id")
            if not isinstance(page_id, (str, int)) or not str(page_id).strip():
                errors.append(f"{label} Confluence chunk requires page_id")
        elif strategy_name == "json":
            json_path = metadata.get("json_path")
            if not isinstance(json_path, str) or not json_path.strip():
                errors.append(f"{label} JSON chunk requires json_path")

    @staticmethod
    def _optional_id(value: object | None) -> str | None:
        return str(value) if value is not None else None
