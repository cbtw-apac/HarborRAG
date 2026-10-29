"""Shared invariants reused by domain dataclass ``__post_init__`` hooks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from harborrag_core.security.field_names import is_sensitive_field_name
from harborrag_core.security.secret_refs import (
    ENV_PREFIX,
    REFERENCE_KEY,
    STORED_PREFIX,
    VAULT_PREFIX,
    is_secret_shaped,
)


def require_id(value: str, *, label: str) -> None:
    """Enforce the common domain ID invariant: non-empty, no whitespace."""
    if not value or any(ch.isspace() for ch in value):
        raise ValueError(f"{label} id must be non-empty and contain no whitespace.")


def require_tenant_id(value: str) -> None:
    require_id(value, label="Tenant")


def require_identity_fields(**fields: str) -> None:
    """Reject an isolation key with a blank component.

    An identity assembled from an upstream bug used to construct happily with
    ``""`` and then key a bucket every such caller shared. Blank is never a
    valid tenant, session, user, run, or principal, so it fails here.
    """

    blank = sorted(name for name, value in fields.items() if not value or not value.strip())
    if blank:
        raise ValueError(f"identity fields must be non-empty: {', '.join(blank)}")


_MAX_CONFIG_DEPTH = 32
_MAX_CONFIG_ITEMS = 100_000


def validate_secret_free_config(value: Mapping[str, Any]) -> None:
    """Reject raw credentials while permitting explicit secret-reference objects."""

    _validate_secret_free_value(value, active=set(), depth=0, visited=[0])


def _validate_secret_free_value(
    value: object,
    *,
    active: set[int],
    depth: int,
    visited: list[int],
) -> None:
    if depth > _MAX_CONFIG_DEPTH:
        raise ValueError("configuration nesting exceeds the security validation limit")
    if not isinstance(value, (Mapping, Sequence)) or isinstance(value, (str, bytes, bytearray)):
        return
    identity = id(value)
    if identity in active:
        raise ValueError("configuration contains a recursive container")
    active.add(identity)
    try:
        items = value.items() if isinstance(value, Mapping) else enumerate(value)
        for raw_key, item in items:
            visited[0] += 1
            if visited[0] > _MAX_CONFIG_ITEMS:
                raise ValueError("configuration exceeds the security validation item limit")
            is_reference = _is_secret_reference(item)
            sensitive = isinstance(value, Mapping) and is_sensitive_field_name(raw_key)
            if sensitive and not is_reference:
                raise ValueError(f"configuration field must use a secret reference: {raw_key}")
            if is_reference:
                continue
            _validate_secret_free_value(
                item,
                active=active,
                depth=depth + 1,
                visited=visited,
            )
    finally:
        active.remove(identity)


def _is_secret_reference(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith(STORED_PREFIX)
    if not is_secret_shaped(value):
        return False
    return str(cast(Mapping[str, Any], value)[REFERENCE_KEY]).startswith(
        (STORED_PREFIX, ENV_PREFIX, VAULT_PREFIX)
    )
