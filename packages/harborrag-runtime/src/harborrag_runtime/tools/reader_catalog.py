"""Exact contracts for the additional MCP reader tools."""

from __future__ import annotations

from harborrag_runtime.reader_contracts import (
    DOCUMENT_CONTEXT_CHUNK_FIELDS,
    DOCUMENT_CONTEXT_LIMIT,
    DOCUMENT_CONTEXT_OUTCOMES,
    EVIDENCE_AVAILABILITIES,
    EVIDENCE_BATCH_LIMIT,
    EVIDENCE_ITEM_FIELDS,
    SOURCE_CONNECTOR_FILTER_LIMIT,
    SOURCE_LIST_LIMIT,
)

from .base import ToolSpec
from .retrieval_inputs import TENANT_PROPERTY, success_or_failure_schema

READ_ONLY_ANNOTATIONS: dict[str, object] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
CONTRACT_REVISION = "reader-v1"

_COMPLETION = {
    "type": "object",
    "required": ["complete", "reasons"],
    "properties": {
        "complete": {"type": "boolean"},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
    },
    "additionalProperties": False,
}
_CITATION = {"type": "object"}
_SECTION_PATH = {"type": "array", "items": {"type": "string"}, "maxItems": 50}


def reader_success_schema(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return success_or_failure_schema(
        {
            "type": "object",
            "required": ["ok", "request_id", "contract_revision", *required, "completion"],
            "properties": {
                "ok": {"const": True},
                "request_id": {"type": "string", "minLength": 1},
                "contract_revision": {"const": CONTRACT_REVISION},
                **properties,
                "completion": _COMPLETION,
            },
            "additionalProperties": False,
        }
    )


_EVIDENCE_ITEM_INPUT = {
    "type": "object",
    "required": ["chunk_id"],
    "properties": {
        "chunk_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "expected_document_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "expected_document_version_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
        },
    },
    "additionalProperties": False,
}
EVIDENCE_ITEM_SCHEMA = {
    "type": "object",
    "required": list(EVIDENCE_ITEM_FIELDS),
    "properties": {
        "chunk_id": {"type": "string", "minLength": 1},
        "availability": {
            "type": "string",
            "enum": list(EVIDENCE_AVAILABILITIES),
        },
        "text": {"type": ["string", "null"]},
        "document_id": {"type": ["string", "null"]},
        "document_version_id": {"type": ["string", "null"]},
        "document_title": {"type": ["string", "null"]},
        "source_scope_id": {"type": ["string", "null"]},
        "connector_type": {"type": ["string", "null"]},
        "chunk_kind": {"type": ["string", "null"]},
        "ordinal": {"type": ["integer", "null"]},
        "section_path": _SECTION_PATH,
        "citation_locator": _CITATION,
    },
    "additionalProperties": False,
}

FETCH_EVIDENCE_SPEC = ToolSpec(
    "fetch_evidence",
    "Fetch up to ten exact current chunks from immutable evidence artifacts. Pass chunk IDs "
    "returned by vector_search and expected document/version IDs when known. Each item is "
    "reauthorized and never silently replaced by a newer version.",
    {
        "type": "object",
        "required": ["tenant_id", "items"],
        "properties": {
            "tenant_id": TENANT_PROPERTY,
            "items": {
                "type": "array",
                "items": _EVIDENCE_ITEM_INPUT,
                "minItems": 1,
                "maxItems": EVIDENCE_BATCH_LIMIT,
            },
        },
        "additionalProperties": False,
    },
    output_schema=reader_success_schema(
        {
            "items": {
                "type": "array",
                "items": EVIDENCE_ITEM_SCHEMA,
                "maxItems": EVIDENCE_BATCH_LIMIT,
            }
        },
        ["items"],
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)

_CONTEXT_CHUNK = {
    "type": "object",
    "required": list(DOCUMENT_CONTEXT_CHUNK_FIELDS),
    "properties": {
        "chunk_id": {"type": "string", "minLength": 1},
        "ordinal": {"type": "integer", "minimum": 0},
        "text": {"type": "string"},
        "chunk_kind": {"type": "string", "minLength": 1},
        "section_path": _SECTION_PATH,
        "citation_locator": _CITATION,
    },
    "additionalProperties": False,
}

GET_DOCUMENT_CONTEXT_SPEC = ToolSpec(
    "get_document_context",
    "Read an ordered, bounded window of chunks from one active document. Anchor by exact "
    "chunk or section path, and continue only with the opaque cursor returned by this tool. "
    "A publication change is reported explicitly; historical versions are not substituted.",
    {
        "type": "object",
        "required": ["tenant_id", "document_id"],
        "properties": {
            "tenant_id": TENANT_PROPERTY,
            "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "expected_document_version_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
            "anchor_chunk_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "anchor_section_path": _SECTION_PATH,
            "cursor": {"type": "string", "pattern": "^cur_"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": DOCUMENT_CONTEXT_LIMIT,
                "default": DOCUMENT_CONTEXT_LIMIT,
            },
            "include_outline": {"type": "boolean", "default": False},
        },
        "allOf": [
            {"not": {"required": ["anchor_chunk_id", "anchor_section_path"]}},
        ],
        "additionalProperties": False,
    },
    output_schema=reader_success_schema(
        {
            "outcome": {
                "type": "string",
                "enum": list(DOCUMENT_CONTEXT_OUTCOMES),
            },
            "document_id": {"type": "string", "minLength": 1},
            "document_version_id": {"type": ["string", "null"]},
            "chunks": {
                "type": "array",
                "items": _CONTEXT_CHUNK,
                "maxItems": DOCUMENT_CONTEXT_LIMIT,
            },
            "outline": {
                "type": "array",
                "items": _SECTION_PATH,
                "maxItems": DOCUMENT_CONTEXT_LIMIT,
            },
            "outline_complete": {"type": "boolean"},
            "next_cursor": {"type": ["string", "null"]},
        },
        [
            "outcome",
            "document_id",
            "document_version_id",
            "chunks",
            "outline",
            "outline_complete",
            "next_cursor",
        ],
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)

_SOURCE = {
    "type": "object",
    "required": [
        "source_id",
        "name",
        "connector_type",
        "ingestion_state",
        "last_source_check_at",
        "last_successful_source_check_at",
        "last_successful_ingestion_at",
        "active_document_count",
    ],
    "properties": {
        "source_id": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "connector_type": {"type": "string", "minLength": 1},
        "ingestion_state": {"type": ["string", "null"]},
        "last_source_check_at": {"type": ["string", "null"], "format": "date-time"},
        "last_successful_source_check_at": {
            "type": ["string", "null"],
            "format": "date-time",
        },
        "last_successful_ingestion_at": {
            "type": ["string", "null"],
            "format": "date-time",
        },
        "active_document_count": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}

LIST_SOURCES_SPEC = ToolSpec(
    "list_sources",
    "List corpus scopes readable by the authenticated principal, with safe connector and "
    "freshness metadata. Source IDs can be used as source_scope_id filters in vector_search "
    "and resolve_graph_nodes. Connection configuration and storage addresses are excluded.",
    {
        "type": "object",
        "required": ["tenant_id"],
        "properties": {
            "tenant_id": TENANT_PROPERTY,
            "source_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": SOURCE_LIST_LIMIT,
                "uniqueItems": True,
            },
            "connector_types": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": SOURCE_CONNECTOR_FILTER_LIMIT,
                "uniqueItems": True,
            },
            "cursor": {"type": "string", "pattern": "^cur_"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": SOURCE_LIST_LIMIT,
                "default": SOURCE_LIST_LIMIT,
            },
        },
        "additionalProperties": False,
    },
    output_schema=reader_success_schema(
        {
            "sources": {
                "type": "array",
                "items": _SOURCE,
                "maxItems": SOURCE_LIST_LIMIT,
            },
            "next_cursor": {"type": ["string", "null"]},
        },
        ["sources", "next_cursor"],
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)

_RESOLVED_NODE = {
    "type": "object",
    "required": [
        "node_key",
        "title",
        "node_kind",
        "entity_type",
        "source_id",
        "content_availability",
    ],
    "properties": {
        "node_key": {"type": "string", "minLength": 1},
        "title": {"type": ["string", "null"]},
        "node_kind": {"type": "string", "minLength": 1},
        "entity_type": {"type": "string", "minLength": 1},
        "source_id": {"type": ["string", "null"]},
        "content_availability": {
            "type": "string",
            "enum": ["evidence", "document_context", "unknown"],
        },
    },
    "additionalProperties": False,
}

RESOLVE_GRAPH_NODES_SPEC = ToolSpec(
    "resolve_graph_nodes",
    "Resolve an exact node key, provider ID, or title to explicit authorized graph node "
    "candidates. Use returned node_key values in graph traversal tools. Results preserve "
    "ambiguity; fuzzy and partial-title matching are not performed.",
    {
        "type": "object",
        "required": ["tenant_id", "selector"],
        "properties": {
            "tenant_id": TENANT_PROPERTY,
            "selector": {
                "type": "object",
                "required": ["kind", "value"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["node_key", "provider_id", "exact_title"],
                    },
                    "value": {"type": "string", "minLength": 1, "maxLength": 512},
                },
                "additionalProperties": False,
            },
            "source_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 10,
                "uniqueItems": True,
            },
            "entity_types": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 10,
                "uniqueItems": True,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "additionalProperties": False,
    },
    output_schema=reader_success_schema(
        {
            "resolution": {
                "type": "string",
                "enum": ["unique", "ambiguous", "no_match"],
            },
            "candidates": {"type": "array", "items": _RESOLVED_NODE, "maxItems": 10},
        },
        ["resolution", "candidates"],
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)

__all__ = [
    "CONTRACT_REVISION",
    "EVIDENCE_ITEM_SCHEMA",
    "FETCH_EVIDENCE_SPEC",
    "GET_DOCUMENT_CONTEXT_SPEC",
    "LIST_SOURCES_SPEC",
    "RESOLVE_GRAPH_NODES_SPEC",
    "READ_ONLY_ANNOTATIONS",
    "reader_success_schema",
]
