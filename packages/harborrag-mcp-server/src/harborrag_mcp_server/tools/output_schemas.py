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
  ``EvidenceResultLoader.result`` (``harborrag_runtime.retrieval.result_loader``); this
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
from harborrag_core.summary_cards import SummaryView

from .evidence_output_schema import ASSERTION_SCHEMA, EVIDENCE_PATH_SCHEMA, NAVIGATION_SCHEMA

_NODE_KIND_VALUES = [kind.value for kind in KnowledgeNodeKind]
_PROJECTED_RELATION_VALUES = [relation.value for relation in PROJECTED_RELATION_TYPES]
_SUMMARY_SCHEMA = SummaryView.model_json_schema()
_SUMMARY_DEFINITIONS = _SUMMARY_SCHEMA.pop("$defs")
_SUMMARY_SCHEMA["properties"]["card"]["anyOf"][0] = _SUMMARY_DEFINITIONS["SummaryCard"]

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
        "document_version_id": {"type": "string"},
        "source_scope_id": {"type": "string"},
        "summary": _SUMMARY_SCHEMA,
    },
    "additionalProperties": False,
}

RELATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "relation_id",
        "relation_type",
        "source_node_key",
        "target_node_key",
        "origin",
    ],
    "properties": {
        "relation_id": {"type": "string", "minLength": 1},
        "relation_type": {"type": "string", "enum": _PROJECTED_RELATION_VALUES},
        "source_node_key": {"type": "string", "minLength": 1},
        "target_node_key": {"type": "string", "minLength": 1},
        "origin": {
            "type": "string",
            "enum": ["structural", "source_declared", "logical_view"],
        },
        "source_scope_id": {"type": "string"},
        "document_id": {"type": "string"},
        "document_version_id": {"type": "string"},
    },
    "additionalProperties": False,
}

TRIPLET_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["subject", "predicate", "relation", "object"],
    "properties": {
        "subject": NODE_SCHEMA,
        "predicate": {"type": "string", "enum": _PROJECTED_RELATION_VALUES},
        "relation": RELATION_SCHEMA,
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
        "short_by": {"type": "integer", "minimum": 0},
        "topology": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["flat", "local_semantic"]},
                "fallback": {"type": ["string", "null"]},
                "seed_entities": {"type": "integer"},
                "assertions": {"type": "integer"},
                "candidates": {"type": "integer"},
                "rejected": {"type": "integer"},
                "ambiguous_labels": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "suppressed_entities": {"type": "integer"},
                "policy_version": {"type": ["string", "null"]},
                "context_tokens": {"type": "integer"},
                "budget_excluded": {"type": "integer"},
            },
            "additionalProperties": False,
        },
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
        # Not required: a lane that cannot measure similarity reports null,
        # and older payloads predate the field. Unlike "score", this is the
        # number a client may threshold -- on the hybrid lane "score" is a
        # rank-fusion value whose top hit is near 1.0 however poor the match.
        "relevance": {"type": ["number", "null"]},
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
                "document_title",
                "section_path",
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
                "raw_score": {"type": "number"},
                "source_scope_id": {"type": "string"},
                "source_item_id": {"type": "string"},
                "document_title": {"type": ["string", "null"]},
                "section_path": {"type": "array", "items": {"type": "string"}},
                "content_hash": {"type": "string"},
                "topology_build_ids": {"type": "array", "items": {"type": "string"}},
                "topology_assertion_ids": {"type": "array", "items": {"type": "string"}},
                "evidence_paths": {"type": "array", "items": EVIDENCE_PATH_SCHEMA},
                "qualified_assertions": {"type": "array", "items": ASSERTION_SCHEMA},
                "derived_artifact_ids": {"type": "array", "items": {"type": "string"}},
                "navigation_summaries": {"type": "array", "items": NAVIGATION_SCHEMA},
                "retrieval_ranks": {
                    "type": "object",
                    "properties": {"flat": {"type": "integer"}, "semantic": {"type": "integer"}},
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}
