"""Shared strict input helpers for tenant-scoped retrieval tools."""

from __future__ import annotations

import math
from collections.abc import Mapping

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext


def text(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HarborValidationError(f"{name} must be a non-empty string")
    return value.strip()


def optional_text(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HarborValidationError(f"{name} must be a non-empty string when provided")
    return value.strip()


def integer(
    arguments: Mapping[str, object],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HarborValidationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise HarborValidationError(f"{name} must be between {minimum} and {maximum}")
    return value


def number(
    arguments: Mapping[str, object],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HarborValidationError(f"{name} must be a number")
    selected = float(value)
    if not math.isfinite(selected) or not minimum <= selected <= maximum:
        raise HarborValidationError(f"{name} must be between {minimum} and {maximum}")
    return selected


def boolean(arguments: Mapping[str, object], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise HarborValidationError(f"{name} must be a boolean")
    return value


def mapping(arguments: Mapping[str, object], name: str) -> dict[str, object]:
    value = arguments.get(name, {})
    if not isinstance(value, Mapping):
        raise HarborValidationError(f"{name} must be an object")
    result = dict(value)
    if "tenant_id" in result:
        raise HarborValidationError("tenant_id must not be duplicated inside filters")
    return result


def string_list(arguments: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = arguments.get(name, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise HarborValidationError(f"{name} must be an array of non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise HarborValidationError(f"{name} must not contain duplicates")
    return normalized


def access(arguments: Mapping[str, object], principal_id: str) -> AccessContext:
    return AccessContext(
        principal_id=principal_id,
        tenant_id=TenantId(text(arguments, "tenant_id")),
    )


TENANT_PROPERTY: dict[str, object] = {
    "type": "string",
    "minLength": 1,
    "description": "Required tenant scope for retrieval.",
}
