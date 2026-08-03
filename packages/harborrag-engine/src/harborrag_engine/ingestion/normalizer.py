"""Canonical normalization between parser output and ingestion services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from typing import Any

from harborrag_core.domain.document import Document, DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.domain.raw_document import RawDocument
from harborrag_engine.ingestion.base import BaseDocumentNormalizer

from .tables import FlatTableArtifactBuilder


class DocumentNormalizer(BaseDocumentNormalizer):
    """Preserve parser structure while merging source-owned metadata."""

    def __init__(
        self,
        table_builder: FlatTableArtifactBuilder | None = None,
    ) -> None:
        self._tables = table_builder or FlatTableArtifactBuilder()

    def normalize(self, raw: RawDocument, parsed: ParsedDocument) -> Document:
        source_metadata = dict(raw.metadata)
        parser_metadata = dict(parsed.metadata or {})
        metadata = {**source_metadata, **parser_metadata}

        elements = list(parsed.elements or ())
        if not elements and parsed.content.strip():
            elements.append(
                DocumentElement(
                    id=f"{raw.id}#content",
                    type="paragraph",
                    content=parsed.content,
                    metadata={"parser_name": parsed.parser_name},
                )
            )

        source_version = self._source_version(raw, metadata)
        tables = self._tables.build(
            elements,
            document_id=raw.id,
            document_version_id=encoded_source_version(
                raw.id,
                source_version,
            ),
            source_version=source_version,
            source_url=raw.source,
        )
        table_by_element = {table.source_block_id: table for table in tables}
        elements = [
            (
                replace(
                    element,
                    metadata={
                        **element.metadata,
                        "table_id": table_by_element[element.id].table_id,
                        "table_version_id": (table_by_element[element.id].table_version_id),
                    },
                )
                if element.id in table_by_element
                else element
            )
            for element in elements
        ]
        return Document(
            id=raw.id,
            title=self._title(raw.id, metadata),
            content=elements,
            content_type=str(metadata.get("content_type") or raw.content_type),
            provenance=self._provenance(raw, metadata, parsed),
            relations=self._relations(metadata.get("relations")),
            raw=self._raw_details(raw, parsed),
            table_artifacts=tables,
        )

    @staticmethod
    def _title(document_id: str, metadata: Mapping[str, Any]) -> str:
        for key in ("title", "name", "filename", "file_name"):
            value = metadata.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return document_id

    @classmethod
    def _provenance(
        cls,
        raw: RawDocument,
        metadata: Mapping[str, Any],
        parsed: ParsedDocument,
    ) -> DocumentProvenance:
        permissions = metadata.get("permissions")
        tags = metadata.get("tags")
        known = {
            "author",
            "checksum",
            "content_type",
            "created_at",
            "file_name",
            "filename",
            "name",
            "permissions",
            "relations",
            "tags",
            "title",
            "updated_at",
            "url",
        }
        extra = {key: value for key, value in metadata.items() if key not in known}
        extra.update(
            {
                "parser_name": parsed.parser_name,
                "parser_version": parsed.parser_version,
                "parser_warnings": list(parsed.warnings or ()),
            }
        )
        return DocumentProvenance(
            source=raw.source,
            record_id=raw.id,
            url=cls._optional_text(metadata.get("url")),
            author=cls._optional_text(metadata.get("author")),
            checksum=cls._optional_text(metadata.get("checksum")),
            permissions=(dict(permissions) if isinstance(permissions, Mapping) else {}),
            created_at=cls._datetime(metadata.get("created_at")),
            updated_at=cls._datetime(metadata.get("updated_at")),
            tags=cls._string_list(tags),
            extra=extra,
        )

    @staticmethod
    def _relations(value: Any) -> list[DocumentRelation]:
        if not isinstance(value, (list, tuple)):
            return []
        relations: list[DocumentRelation] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            predicate = item.get("predicate")
            target_id = item.get("target_id")
            target_type = item.get("target_type")
            if not all(
                candidate is not None and str(candidate).strip()
                for candidate in (predicate, target_id, target_type)
            ):
                continue
            relation_metadata = item.get("metadata")
            relations.append(
                DocumentRelation(
                    predicate=str(predicate),
                    target_id=str(target_id),
                    target_type=str(target_type),
                    metadata=(
                        dict(relation_metadata) if isinstance(relation_metadata, Mapping) else {}
                    ),
                )
            )
        return relations

    @staticmethod
    def _raw_details(raw: RawDocument, parsed: ParsedDocument) -> dict[str, Any]:
        details = dict(raw.raw or {})
        if parsed.raw:
            details["parser"] = dict(parsed.raw)
        return details

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None or not str(value).strip():
            return None
        return str(value).strip()

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value] if value.strip() else []
        if not isinstance(value, (list, tuple, set, frozenset)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _source_version(
        raw: RawDocument,
        metadata: Mapping[str, Any],
    ) -> str:
        for key in ("source_version", "version", "checksum", "updated_at"):
            value = metadata.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        content = raw.content.encode("utf-8") if isinstance(raw.content, str) else raw.content
        return sha256(content).hexdigest()


def encoded_source_version(document_id: str, source_version: str) -> str:
    """Build the pre-planning table version context deterministically."""

    return sha256(f"{document_id}\0{source_version}".encode()).hexdigest()
