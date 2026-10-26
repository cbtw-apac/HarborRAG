"""The one relationship between a graph node's storage label and its semantic subtype.

``node_kind`` is the storage label: Cypher labels cannot be unbounded, so the set is
closed and small. ``entity_type`` is the subtype that label cannot express. Keeping both
is deliberate, but the relationship between them is not free-form:

* four kinds admit exactly one subtype, so it is derived rather than restated;
* ``STRUCTURE`` draws from a closed structural vocabulary;
* ``SOURCE_ENTITY`` stays open to new connectors and is only barred from the subtypes
  the other kinds own.
"""

from __future__ import annotations

from .states import GraphEntityType, KnowledgeNodeKind

FORCED_ENTITY_TYPE = {
    KnowledgeNodeKind.TENANT: GraphEntityType.TENANT,
    KnowledgeNodeKind.DATA_SOURCE: GraphEntityType.DATA_SOURCE,
    KnowledgeNodeKind.DOCUMENT_VERSION: GraphEntityType.DOCUMENT_VERSION,
    KnowledgeNodeKind.CHUNK: GraphEntityType.CHUNK,
}

STRUCTURAL_ENTITY_TYPES = frozenset(
    {GraphEntityType.SECTION, GraphEntityType.TABLE, GraphEntityType.COMMENT}
)

RESERVED_ENTITY_TYPES = frozenset(FORCED_ENTITY_TYPE.values()) | STRUCTURAL_ENTITY_TYPES


def derived_entity_type(node_kind: object) -> GraphEntityType | None:
    """Return the only legal subtype for a kind that admits one, else ``None``."""

    if not isinstance(node_kind, str | KnowledgeNodeKind):
        return None
    try:
        kind = KnowledgeNodeKind(node_kind)
    except ValueError:
        return None
    return FORCED_ENTITY_TYPE.get(kind)


def validate_entity_type(node_kind: KnowledgeNodeKind, entity_type: GraphEntityType) -> None:
    """Reject a subtype the kind cannot carry."""

    forced = FORCED_ENTITY_TYPE.get(node_kind)
    if forced is not None and entity_type != forced:
        raise ValueError(f"{node_kind.value} nodes require entity_type={forced.value}")
    if node_kind == KnowledgeNodeKind.STRUCTURE and entity_type not in STRUCTURAL_ENTITY_TYPES:
        raise ValueError(
            "structure nodes require one of "
            f"{sorted(item.value for item in STRUCTURAL_ENTITY_TYPES)}"
        )
    if node_kind == KnowledgeNodeKind.SOURCE_ENTITY and entity_type in RESERVED_ENTITY_TYPES:
        raise ValueError(
            f"source_entity nodes carry a provider type, never the reserved {entity_type.value!r}"
        )
