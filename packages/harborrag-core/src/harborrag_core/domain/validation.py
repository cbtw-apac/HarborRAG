"""Shared invariants reused by domain dataclass ``__post_init__`` hooks.

Error messages never echo the rejected value, because source item IDs can hold
file names and configuration can hold credentials.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence

from harborrag_core.security.field_names import is_sensitive_field_name
from harborrag_core.security.secret_refs import (
    ENV_PREFIX,
    REFERENCE_KEY,
    STORED_PREFIX,
    VAULT_PREFIX,
    is_secret_shaped,
)

_CONTROL_CHARACTER_CATEGORY = "Cc"
_MAX_CONFIG_DEPTH = 32
_MAX_CONFIG_ITEMS = 100_000


def require_id(value: str, *, label: str) -> None:
    """Reject an internal domain ID that is empty or holds any whitespace.

    Args:
        value: ID to check.
        label: Entity name for the error message, such as ``"Job"``.

    Raises:
        ValueError: The ID is empty or holds whitespace.
    """
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"{label} id must be non-empty and contain no whitespace.")


def require_source_item_id(value: str, *, label: str) -> None:
    """Reject a source item ID that is blank or holds a control character.

    Connectors build these IDs from file paths, so a space in a file or folder
    name is kept, as in ``docs/Q3 design review.md``. Line breaks, tabs and
    other control characters are still rejected.

    Args:
        value: Source item ID to check.
        label: Entity name for the error message, such as ``"Document"``.

    Raises:
        ValueError: The ID is blank or holds a control character.
    """
    if not value.strip() or any(_is_control_character(character) for character in value):
        raise ValueError(f"{label} id must be non-empty and contain no control characters.")


def require_tenant_id(value: str) -> None:
    """Reject a tenant ID that is empty or holds any whitespace."""
    require_id(value, label="Tenant")


def require_identity_fields(**fields: str) -> None:
    """Reject an isolation key with a blank component.

    Blank is never a valid tenant, session, user, run or principal. Accepting it
    would key one bucket that every caller with the same upstream bug shares.

    Args:
        **fields: Component values of the isolation key, keyed by field name.

    Raises:
        ValueError: A component is empty or blank.
    """
    blank = sorted(name for name, value in fields.items() if not value or not value.strip())
    if blank:
        raise ValueError(f"identity fields must be non-empty: {', '.join(blank)}")


def validate_secret_free_config(value: Mapping[str, object]) -> None:
    """Reject raw credentials while permitting explicit secret-reference objects.

    Args:
        value: Configuration to check, walked through nested mappings and lists.

    Raises:
        ValueError: A sensitive field holds a raw value instead of a secret
            reference, or the configuration is recursive, too deep or too large.
    """
    _validate_secret_free_value(value, active=set(), depth=0, visited=[0])


def _is_control_character(character: str) -> bool:
    return unicodedata.category(character) == _CONTROL_CHARACTER_CATEGORY


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
    if not is_secret_shaped(value) or not isinstance(value, Mapping):
        return False
    return str(value[REFERENCE_KEY]).startswith((STORED_PREFIX, ENV_PREFIX, VAULT_PREFIX))
