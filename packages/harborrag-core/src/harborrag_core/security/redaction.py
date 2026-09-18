from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Kept in step with ``SENSITIVE_FIELD_TOKENS``: this matches free text rather
# than field names, so it stays a pattern, but a name the set knows and this
# does not is a credential that reaches a log.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)api[_-]?key|access[_-]?key|private[_-]?key|token(?!s(?![a-z])|izer)|secret|passwd|password|credential|authorization"
)

# The unquoted branch stops before either a delimiter (",", ";", "&", "}", "]",
# a line break) or the start of the next "key[:=]" pair, so a multi-word secret
# ("password: My Secret Passphrase 123") is redacted in full while an adjacent
# field on the same line ("api_key=abc123&user=alice") survives.
_UNQUOTED_VALUE = r"(?:(?!\s+[\w.-]+\s*[:=]).)+?(?=[,;&}\]\r\n]|\s+[\w.-]+\s*[:=]|$)"
_VALUE = rf'(?:"[^"]*"|\'[^\']*\'|{_UNQUOTED_VALUE})'

# Authorization carries a scheme before the credential ("Bearer x", "Basic y"),
# and the generic value branch below stops at what looks like the next "key="
# pair -- which base64 padding does. So it gets its own branch that consumes to
# a real delimiter, placed first. Previously only the "Bearer" form was handled
# at all, so a Basic credential went to the log intact.
_AUTHORIZATION_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^,;&}\]\r\n]+)"

_LABELED_PATTERNS = [
    re.compile(r"(?i)(authorization)['\"]?\s*[:=]\s*" + _AUTHORIZATION_VALUE),
    re.compile(
        # The key group spans its whole name so the replacement preserves it:
        # redacting "api_key_id" must not rewrite the key to "api_key".
        # ``token`` carries the same lookahead as _SENSITIVE_KEY_PATTERN so a
        # token *count* ("tokens=5") is left alone.
        r"(?i)((?:api[_-]?key|access[_-]?key|private[_-]?key|token(?!s(?![a-z])|izer)"
        r"|secret|passwd|password|credential)[\w-]*)"
        r"['\"]?\s*[:=]\s*" + _VALUE
    ),
]

_TOKEN_PATTERNS = [
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
]


def redact_secrets(text: str, replacement: str = "<redacted>") -> str:
    result = text
    for pattern in _LABELED_PATTERNS:
        result = pattern.sub(lambda match: f"{match.group(1)}={replacement}", result)
    for pattern in _TOKEN_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _redact_value(value: Any, replacement: str) -> Any:
    if isinstance(value, Mapping):
        return redact_mapping(value, replacement)
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item, replacement) for item in value)
    if isinstance(value, str):
        return redact_secrets(value, replacement)
    return value


def redact_mapping(data: Mapping[str, Any], replacement: str = "<redacted>") -> dict[str, Any]:
    """Recursively mask values whose key looks credential-shaped.

    Defense-in-depth for DTO boundaries that serialize free-form config
    (e.g. SourceConfig.config): a key matching _SENSITIVE_KEY_PATTERN is
    masked regardless of what invariants upstream write-side code is
    supposed to enforce. Mappings nested inside lists/tuples are recursed
    into, and string values are additionally passed through
    redact_secrets() to catch secrets embedded in free-form text (e.g. a
    header line under a non-sensitive key).
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY_PATTERN.search(key):
            result[key] = replacement
        else:
            result[key] = _redact_value(value, replacement)
    return result
