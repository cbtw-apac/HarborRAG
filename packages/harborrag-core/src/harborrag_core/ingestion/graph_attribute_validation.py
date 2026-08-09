"""Validation for bounded, non-sensitive graph projection metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from .identity import reject_runtime_fields

_MAX_GRAPH_ATTRIBUTES = 32
_MAX_ATTRIBUTE_KEY_LENGTH = 64
_MAX_ATTRIBUTE_TEXT_LENGTH = 1_024
_MAX_ATTRIBUTE_SEQUENCE = 32
_MAX_ATTRIBUTE_DEPTH = 4
_FORBIDDEN_ATTRIBUTE_TOKENS = frozenset(
    {
        "access_token",
        "api_key",
        "body",
        "content",
        "credential",
        "credentials",
        "password",
        "payload",
        "preview",
        "raw",
        "secret",
        "text",
        "token",
    }
)
_ALLOWED_ATTRIBUTE_FIELDS = frozenset(
    {
        "connector_type",
        "connection_id",
        "ctag",
        "default_branch",
        "display_name",
        "document_kind",
        "drive_type",
        "etag",
        "issue_type",
        "issue_key",
        "item_name",
        "mode",
        "name",
        "ordinal",
        "page_id",
        "parent_relative_path",
        "placeholder",
        "project_key",
        "provider_id",
        "provider_key",
        "relative_path",
        "resolved_at",
        "sha",
        "source_item_id",
        "source_uri",
        "space_key",
        "status",
        "suffix",
    }
)


def validate_graph_attributes(attributes: Mapping[str, Any]) -> None:
    """Reject sensitive, unbounded, or non-JSON graph metadata."""

    _validate_attribute_mapping(attributes, depth=0)


def _validate_attribute_mapping(attributes: Mapping[str, Any], *, depth: int) -> None:
    if len(attributes) > _MAX_GRAPH_ATTRIBUTES:
        raise ValueError(f"graph attributes may contain at most {_MAX_GRAPH_ATTRIBUTES} entries")
    reject_runtime_fields(attributes)
    for key, value in attributes.items():
        normalized = str(key).strip().casefold().replace("-", "_")
        if not normalized or len(normalized) > _MAX_ATTRIBUTE_KEY_LENGTH:
            raise ValueError("graph attribute keys must be bounded non-empty text")
        tokens = set(normalized.split("_"))
        if normalized in _FORBIDDEN_ATTRIBUTE_TOKENS or tokens & _FORBIDDEN_ATTRIBUTE_TOKENS:
            raise ValueError(f"graph attribute field is not allowed: {key}")
        if normalized not in _ALLOWED_ATTRIBUTE_FIELDS:
            raise ValueError(f"graph attribute field is not allowlisted: {key}")
        _validate_attribute_value(value, depth=depth)


def _validate_attribute_value(value: Any, *, depth: int) -> None:
    if depth > _MAX_ATTRIBUTE_DEPTH:
        raise ValueError("graph attribute nesting exceeds the bounded metadata depth")
    if _is_valid_attribute_scalar(value):
        return
    if isinstance(value, str):
        if len(value) > _MAX_ATTRIBUTE_TEXT_LENGTH:
            raise ValueError("graph attribute text exceeds the bounded metadata limit")
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_GRAPH_ATTRIBUTES:
            raise ValueError("graph attribute mappings must be shallow and bounded")
        _validate_attribute_mapping(value, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > _MAX_ATTRIBUTE_SEQUENCE:
            raise ValueError("graph attribute sequences exceed the bounded metadata limit")
        for item in value:
            _validate_attribute_value(item, depth=depth + 1)
        return
    raise ValueError("graph attributes support JSON-compatible metadata values only")


def _is_valid_attribute_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if not isinstance(value, float):
        return False
    if not isfinite(value):
        raise ValueError("graph attribute numbers must be finite")
    return True
