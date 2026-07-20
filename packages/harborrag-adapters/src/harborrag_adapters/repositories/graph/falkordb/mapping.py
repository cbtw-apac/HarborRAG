from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from harborrag_core.schemas.graph import GraphEdge, GraphNode
from harborrag_core.schemas.ids import TenantId


class FalkorDBMapper:
    """Validates FalkorDB identifiers and maps graph values into core schemas."""

    identifier_pattern: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
    json_prefix: ClassVar[str] = "__harbor_json_v1__:"

    @classmethod
    def safe_identifier(cls, value: str) -> str:
        """Return a relationship identifier safe for controlled Cypher interpolation."""
        normalized = value.upper()
        if not cls.identifier_pattern.fullmatch(normalized):
            raise ValueError(
                "relationship types must match ^[A-Z][A-Z0-9_]{0,63}$ after upper-casing"
            )
        return normalized

    @classmethod
    def encode_properties(cls, properties: dict[str, Any]) -> dict[str, Any]:
        """Encode structured values that FalkorDB cannot store as properties."""
        return {key: cls.encode_property(value) for key, value in properties.items()}

    @classmethod
    def encode_property(cls, value: Any) -> Any:
        """Preserve primitive provider values and mark JSON-encoded structures."""
        primitive = value is None or isinstance(value, (bool, int, float))
        primitive_list = isinstance(value, list) and all(
            item is None or isinstance(item, (bool, int, float, str)) for item in value
        )
        safe_string = isinstance(value, str) and not value.startswith(cls.json_prefix)
        if primitive or primitive_list or safe_string:
            return value
        serialized = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
        return f"{cls.json_prefix}{serialized}"

    @classmethod
    def decode_property(cls, value: Any) -> Any:
        """Decode values marked by :meth:`encode_property`."""
        if not isinstance(value, str) or not value.startswith(cls.json_prefix):
            return value
        return json.loads(value.removeprefix(cls.json_prefix))

    @classmethod
    def node(cls, raw: Any, tenant_id: TenantId) -> GraphNode:
        """Convert a FalkorDB node while separating HarborRAG metadata fields."""
        properties = {
            key: cls.decode_property(value)
            for key, value in dict(getattr(raw, "properties", raw)).items()
        }
        payload: dict[str, Any] = {
            "id": properties.pop("id"),
            "tenant_id": tenant_id,
            "labels": set(properties.pop("labels", getattr(raw, "labels", []))),
            "confidence": properties.pop("confidence", None),
            "provenance": properties.pop("provenance", {}),
        }
        properties.pop("tenant_id", None)
        valid_from = properties.pop("valid_from", None)
        valid_to = properties.pop("valid_to", None)
        payload["properties"] = properties
        if valid_from:
            payload["valid_from"] = valid_from
        if valid_to:
            payload["valid_to"] = valid_to
        return GraphNode.model_validate(payload)

    @classmethod
    def edge(cls, raw: Any, tenant_id: TenantId) -> GraphEdge:
        """Convert a FalkorDB edge and preserve both endpoint identities."""
        properties = {
            key: cls.decode_property(value)
            for key, value in dict(getattr(raw, "properties", raw)).items()
        }
        payload: dict[str, Any] = {
            "id": properties.pop("id"),
            "tenant_id": tenant_id,
            "source_id": properties.pop("source_id"),
            "target_id": properties.pop("target_id"),
            "relationship_type": getattr(raw, "relation", properties.pop("type", "RELATED_TO")),
            "confidence": properties.pop("confidence", None),
            "provenance": properties.pop("provenance", {}),
        }
        properties.pop("tenant_id", None)
        valid_from = properties.pop("valid_from", None)
        valid_to = properties.pop("valid_to", None)
        payload["properties"] = properties
        if valid_from:
            payload["valid_from"] = valid_from
        if valid_to:
            payload["valid_to"] = valid_to
        return GraphEdge.model_validate(payload)

    @classmethod
    def rows(cls, result: Any) -> list[dict[str, Any]]:
        """Convert FalkorDB tabular results into stable named dictionaries."""
        names = [cls._header_name(item, index) for index, item in enumerate(result.header)]
        return [dict(zip(names, row, strict=False)) for row in result.result_set]

    @staticmethod
    def _header_name(item: Any, index: int) -> str:
        if isinstance(item, (list, tuple)) and len(item) > 1:
            return str(item[1])
        name = getattr(item, "name", None)
        return str(name) if name else f"column_{index}"
