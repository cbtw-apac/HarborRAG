"""Shared invariants reused by domain dataclass ``__post_init__`` hooks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.security.field_names import canonical_field_name, canonical_field_tokens

_SENSITIVE_CONFIG_TOKENS = frozenset(
    {
        "api_key",
        "access_key",
        "access_token",
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
    }
)


def require_id(value: str, *, label: str) -> None:
    """Enforce the common domain ID invariant: non-empty, no whitespace."""
    if not value or any(ch.isspace() for ch in value):
        raise ValueError(f"{label} id must be non-empty and contain no whitespace.")


def require_tenant_id(value: str) -> None:
    require_id(value, label="Tenant")


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
            sensitive = False
            if isinstance(value, Mapping):
                key = canonical_field_name(raw_key)
                tokens = canonical_field_tokens(raw_key)
                sensitive = key in _SENSITIVE_CONFIG_TOKENS or bool(
                    tokens & _SENSITIVE_CONFIG_TOKENS
                )
            if sensitive and not _is_secret_reference(item):
                raise ValueError(f"configuration field must use a secret reference: {raw_key}")
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
        return value.startswith("secret://")
    if not isinstance(value, Mapping) or set(value) != {"secret_ref"}:
        return False
    reference = value.get("secret_ref")
    return isinstance(reference, str) and reference.startswith("secret://")
