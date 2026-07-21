from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .element import DocumentElement
from .provenance import DocumentProvenance
from .validation import require_id


@dataclass(slots=True, kw_only=True)
class DocumentRelation:
    """A structural edge from this document to another, derived directly from source data."""

    predicate: str = field(
        metadata={
            "description": "The type of relationship, e.g. 'parent_of', 'child_of', 'has_attachment', etc."
        }
    )
    target_id: str = field(
        metadata={
            "description": "The ID of the target document or entity, e.g. confluence://SPACE/123 for a Confluence page, or an external ID for a person or tag."
        }
    )
    target_type: str = field(
        metadata={
            "description": "The type of the target entity, e.g. 'document', 'person', 'tag', 'space', etc."
        }
    )
    metadata: dict[str, Any] = field(
        default_factory=dict,
        metadata={
            "description": "Additional metadata for the relationship, e.g. {'link_text': '...'} for a links_to edge"
        },
    )


@dataclass(slots=True, kw_only=True)
class Document:
    """Canonical normalized document passed between ingestion stages."""

    id: str = field(
        metadata={"description": "Globally unique document ID, e.g. confluence://SPACE/123"}
    )
    title: str = field(metadata={"description": "Human-readable title of the document"})
    content: list[DocumentElement] = field(
        metadata={
            "description": "List of structured content elements that make up the document, e.g. headings, paragraphs, tables, images, etc."
        }
    )
    content_type: str = field(
        metadata={
            "description": "Type of content, e.g. 'page', 'blogpost', 'comment', 'attachment', etc."
        }
    )
    provenance: DocumentProvenance = field(
        metadata={"description": "Origin, access, and source-timestamp information"}
    )
    relations: list[DocumentRelation] = field(
        default_factory=list,
        metadata={
            "description": "List of structural edges from this document to others, derived from source data"
        },
    )
    raw: dict[str, Any] | None = field(
        default=None,
        metadata={
            "description": "Raw source data for the document, if needed for debugging or additional processing"
        },
    )

    def __post_init__(self) -> None:
        require_id(self.id, label="Document")
