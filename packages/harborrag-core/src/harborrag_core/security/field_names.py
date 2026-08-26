"""Canonicalize untrusted field names for security-policy comparisons."""

from __future__ import annotations

import re

_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]+")


def canonical_field_name(value: object) -> str:
    """Return a separator- and camel-case-insensitive field name."""

    text = _CAMEL_CASE_BOUNDARY.sub("_", str(value).strip())
    return _NON_ALPHANUMERIC.sub("_", text).strip("_").casefold()


def canonical_field_tokens(value: object) -> frozenset[str]:
    """Return the security-significant tokens in one field name."""

    name = canonical_field_name(value)
    return frozenset(token for token in name.split("_") if token)


__all__ = ["canonical_field_name", "canonical_field_tokens"]
