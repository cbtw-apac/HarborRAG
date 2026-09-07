"""Shared, fully-nested JSON Schema fragments for retrieval tool outputs.

Every fragment here mirrors one real Python shape exactly, down to which keys are
required and which are merely optional:

- ``NODE_SCHEMA`` / ``RELATION_SCHEMA`` -- ``compact_node`` / ``compact_relation``
  (``harborrag_core.retrieval.graph``), the projection every graph tool returns.
- ``TRIPLET_SCHEMA`` -- ``compact_triplet``.
- ``PATH_SCHEMA`` -- ``compact_path`` (``GraphPath`` requires at least 2 nodes and 1
  relation; ``minItems`` mirrors that).
- ``GRAPH_SEARCH_DIAGNOSTICS_SCHEMA`` -- ``GraphSearchDiagnostics``
  (``harborrag_engine.retrieval.graph``), the diagnostics behind every graph tool.
- ``RETRIEVAL_RESULT_SCHEMA`` -- ``RetrievalResult`` as built by
  ``RuntimeRetrievalService._load_result`` (``harborrag_runtime.retrieval.service``); this
  is the only place a production ``vector_search`` response constructs one, so its
  ``metadata`` keys are exhaustive, not illustrative.
- ``RETRIEVAL_DIAGNOSTICS_SCHEMA`` -- ``RetrievalDiagnostics``
  (``harborrag_runtime.retrieval.contracts``), including its nested
  ``graph_documents``/``related_results`` (``GraphDocumentSummary`` /
  ``GraphResultNeighborhood``), which reuse the same node/relation projection.

``entity_type`` is deliberately left as an open string (no ``enum``): unlike
``node_kind`` (a closed ``KnowledgeNodeKind`` StrEnum), ``GraphEntityType`` accepts
dynamically minted ``CUSTOM_*`` members for provider-specific source items via its
``_missing_`` hook, so a real node's ``entity_type`` is not limited to the built-in list.
"""

from __future__ import annotations

from harborrag_core.chunking import PROJECTED_RELATION_TYPES
from harborrag_core.ingestion import KnowledgeNodeKind

_NODE_KIND_VALUES = [kind.value for kind in KnowledgeNodeKind]
_PROJECTED_RELATION_VALUES = [relation.value for relation in PROJECTED_RELATION_TYPES]

NODE_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["node_key", "node_kind", "entity_type"],
    "properties": {
        "node_key": {"type": "string", "minLength": 1},
        "node_kind": {"type": "string", "enum": _NODE_KIND_VALUES},
        "entity_type": {"type": "string", "minLength": 1},
        "title": {"type": "string"},
        "section_path": {"type": "array", "items": {"type": "string"}},
        "document_id": {"type": "string"},
    },
    "additionalProperties": False,
}

RELATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["relation_type", "source_node_key", "target_node_key"],
    "properties": {
        "relation_type": {"type": "string", "enum": _PROJECTED_RELATION_VALUES},
        "source_node_key": {"type": "string", "minLength": 1},
        "target_node_key": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

TRIPLET_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["subject", "predicate", "object"],
    "properties": {
        "subject": NODE_SCHEMA,
        "predicate": {"type": "string", "enum": _PROJECTED_RELATION_VALUES},
        "object": NODE_SCHEMA,
    },
    "additionalProperties": False,
}

PATH_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["nodes", "relations"],
    "properties": {
        "nodes": {"type": "array", "items": NODE_SCHEMA, "minItems": 2},
        "relations": {"type": "array", "items": RELATION_SCHEMA, "minItems": 1},
    },
    "additionalProperties": False,
}

GRAPH_SEARCH_DIAGNOSTICS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "candidate_count",
        "accepted_count",
        "stale_count",
        "unpublished_count",
        "projection_truncated",
    ],
    "properties": {
        "candidate_count": {"type": "integer"},
        "accepted_count": {"type": "integer"},
        "stale_count": {"type": "integer"},
        "unpublished_count": {"type": "integer"},
        "projection_truncated": {"type": "boolean"},
    },
    "additionalProperties": False,
}

_GRAPH_RESULT_NEIGHBORHOOD_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["result_id", "nodes", "relations"],
    "properties": {
        "result_id": {"type": "string", "minLength": 1},
        "nodes": {"type": "array", "items": NODE_SCHEMA},
        "relations": {"type": "array", "items": RELATION_SCHEMA},
    },
    "additionalProperties": False,
}

_GRAPH_DOCUMENT_SUMMARY_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["document_id", "title", "sections", "related_results"],
    "properties": {
        "document_id": {"type": "string", "minLength": 1},
        "title": {"type": ["string", "null"]},
        "sections": {"type": "array", "items": {"type": "string"}},
        "related_results": {"type": "array", "items": _GRAPH_RESULT_NEIGHBORHOOD_SCHEMA},
    },
    "additionalProperties": False,
}

RETRIEVAL_DIAGNOSTICS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "candidate_hits",
        "stale_candidates",
        "unpublished_candidates",
        "malformed_candidates",
        "search_window",
        "graph_nodes",
        "graph_relations",
        "graph_truncated",
        "duration_ms",
        "graph_documents",
    ],
    "properties": {
        "candidate_hits": {"type": "integer"},
        "stale_candidates": {"type": "integer"},
        "unpublished_candidates": {"type": "integer"},
        "malformed_candidates": {"type": "integer"},
        "search_window": {"type": "integer"},
        "graph_nodes": {"type": "integer"},
        "graph_relations": {"type": "integer"},
        "graph_truncated": {"type": "boolean"},
        "duration_ms": {"type": "number"},
        "graph_documents": {"type": "array", "items": _GRAPH_DOCUMENT_SUMMARY_SCHEMA},
    },
    "additionalProperties": False,
}

RETRIEVAL_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["id", "text", "score", "metadata"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "text": {"type": "string"},
        "score": {"type": "number"},
        "metadata": {
            "type": "object",
            "required": [
                "document_id",
                "document_version_id",
                "record_kind",
                "chunk_kind",
                "connector_type",
                "citation_locator",
                "quality_score",
                "retrieval_source",
            ],
            "properties": {
                "document_id": {"type": "string"},
                "document_version_id": {"type": "string"},
                "record_kind": {"type": "string"},
                "chunk_kind": {"type": "string"},
                "connector_type": {"type": "string"},
                # Connector-specific locator shape; genuinely open, unlike everything
                # else here -- there is no fixed set of keys to close over.
                "citation_locator": {"type": "object"},
                "quality_score": {"type": ["number", "null"]},
                "retrieval_source": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}
