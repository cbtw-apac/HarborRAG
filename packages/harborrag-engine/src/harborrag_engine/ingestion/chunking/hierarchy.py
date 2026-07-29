from __future__ import annotations

from collections.abc import Sequence
from unicodedata import normalize

from harborrag_core.chunking import ChunkRecord

from .errors import ChunkHierarchyError


def normalize_section_path(section_path: Sequence[str]) -> tuple[str, ...]:
    """Normalize ordered section titles without changing case or ancestry."""

    normalized: list[str] = []
    for part in section_path:
        value = " ".join(normalize("NFC", part).split())
        if not value:
            raise ChunkHierarchyError("section path parts must be non-empty")
        normalized.append(value)
    return tuple(normalized)


def parent_section_path(section_path: Sequence[str]) -> tuple[str, ...] | None:
    """Return the normalized parent path, or none for a root section."""

    normalized = normalize_section_path(section_path)
    return normalized[:-1] if normalized else None


class ChunkHierarchyValidator:
    """Validate ordered chunk ancestry, ordinals, and neighbor references."""

    def validate(self, records: Sequence[ChunkRecord]) -> None:
        """Raise a domain error when hierarchy relationships are inconsistent."""

        known_ids = {
            identifier
            for record in records
            for identifier in (str(record.chunk_id), str(record.logical_chunk_id))
        }
        seen_ordinals: set[tuple[str, int]] = set()
        for index, record in enumerate(records):
            parent = self._parent_identity(record)
            ordinal_key = (parent, record.ordinal)
            if ordinal_key in seen_ordinals:
                raise ChunkHierarchyError(
                    f"duplicate ordinal {record.ordinal} within parent {parent!r}"
                )
            seen_ordinals.add(ordinal_key)
            self._validate_reference(
                record.hierarchy.previous_chunk_id,
                known_ids,
                "previous_chunk_id",
            )
            self._validate_reference(
                record.hierarchy.next_chunk_id,
                known_ids,
                "next_chunk_id",
            )
            expected_previous = records[index - 1].logical_chunk_id if index else None
            expected_next = (
                records[index + 1].logical_chunk_id if index + 1 < len(records) else None
            )
            if record.hierarchy.previous_chunk_id != expected_previous:
                raise ChunkHierarchyError("previous_chunk_id does not match source order")
            if record.hierarchy.next_chunk_id != expected_next:
                raise ChunkHierarchyError("next_chunk_id does not match source order")

    @staticmethod
    def _parent_identity(record: ChunkRecord) -> str:
        hierarchy = record.hierarchy
        return str(hierarchy.parent_chunk_id or hierarchy.parent_section_id or record.document_id)

    @staticmethod
    def _validate_reference(
        reference: object | None,
        known_ids: set[str],
        label: str,
    ) -> None:
        if reference is not None and str(reference) not in known_ids:
            raise ChunkHierarchyError(f"{label} references an unknown chunk")
