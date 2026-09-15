"""Canonical evidence metadata schemas, inlined for safe nesting in MCP results."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from harborrag_core.topology.records import CanonicalAssertion
from harborrag_core.topology.search import EvidencePath


def _inline(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs", {})

    def expand(value: Any) -> Any:
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return expand(definitions[value["$ref"].removeprefix("#/$defs/")])
        return {key: expand(item) for key, item in value.items() if key != "$defs"}

    return dict(expand(schema))


ASSERTION_SCHEMA = _inline(CanonicalAssertion.model_json_schema())
EVIDENCE_PATH_SCHEMA = _inline(TypeAdapter(EvidencePath).json_schema())
EVIDENCE_PATH_SCHEMA["additionalProperties"] = False

NAVIGATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "parent_key": {"type": ["string", "null"]},
        "description": {"type": "string"},
        "level": {"type": ["string", "null"]},
        "derived_artifact_id": {"type": "string"},
        "coverage": {"type": "string"},
        "input_chunk_count": {"type": "integer"},
    },
    "additionalProperties": False,
}
