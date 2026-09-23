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


SENSITIVE_FIELD_TOKENS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "authorization",
        "credential",
        "passwd",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
"""Field names and name fragments that must never carry a raw value.

One definition because there used to be three -- configuration validation,
model-error sanitisation, and text redaction each kept their own, and they had
already drifted: only the redaction pattern knew ``private_key`` and none knew
``passwd``. A credential that one of them recognises and another does not is a
credential that reaches a log or a database row.
"""

_COUNT_QUALIFIERS = frozenset(
    {
        "budget",
        "cap",
        "ceiling",
        "chunk",
        "count",
        "estimate",
        "limit",
        "max",
        "min",
        "overlap",
        "per",
        "quota",
        "size",
        "total",
        "usage",
        "window",
    }
)
"""Words that make a neighbouring ``token`` a quantity rather than a credential.

``token`` has two unrelated meanings here: the bearer kind and the LLM
accounting kind. Matching it blindly made ``token_budget`` unstorable -- it had
to "use a secret reference" -- while ``max_tokens`` passed only because the
plural happens not to match. Nothing else in the set is ambiguous, so this
exception is deliberately narrow and applies to ``token`` alone.
"""


_SINGLE_WORD_TOKENS = frozenset(t for t in SENSITIVE_FIELD_TOKENS if "_" not in t)
_COMPOUND_TOKENS = frozenset(t for t in SENSITIVE_FIELD_TOKENS if "_" in t)


def is_sensitive_field_name(value: object) -> bool:
    """Whether a field name claims to carry a credential.

    A compound entry is matched anywhere in the name at token boundaries, so
    ``ssh_private_key`` and ``my_api_key`` are caught. Comparing the whole
    canonical name against the set, as this used to, recognised ``api_key``
    but not ``my_api_key`` -- a prefix was all it took to store a raw key.
    """

    name = canonical_field_name(value)
    padded = f"_{name}_"
    if any(f"_{entry}_" in padded for entry in _COMPOUND_TOKENS):
        return True
    tokens = canonical_field_tokens(value)
    matched = tokens & _SINGLE_WORD_TOKENS
    if matched == {"token"} and tokens & _COUNT_QUALIFIERS:
        return False
    return bool(matched)


__all__ = [
    "SENSITIVE_FIELD_TOKENS",
    "canonical_field_name",
    "canonical_field_tokens",
    "is_sensitive_field_name",
]
