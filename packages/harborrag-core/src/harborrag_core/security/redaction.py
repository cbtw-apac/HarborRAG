from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEY_PATTERN = re.compile(r"(?i)api[_-]?key|token|secret|password|credential")

_LABELED_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|credential)['\"]?\s*[:=]\s*"
        r"['\"]?([^\s,'\";}\]]+)"
    ),
    re.compile(r"(?i)(authorization)['\"]?\s*:\s*['\"]?\s*bearer\s+" r"([^\s,'\";}\]]+)"),
]

_TOKEN_PATTERNS = [
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
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


def redact_mapping(data: Mapping[str, Any], replacement: str = "<redacted>") -> dict[str, Any]:
    """Recursively mask values whose key looks credential-shaped.

    Defense-in-depth for DTO boundaries that serialize free-form config
    (e.g. SourceConfig.config): a key matching _SENSITIVE_KEY_PATTERN is
    masked regardless of what invariants upstream write-side code is
    supposed to enforce.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY_PATTERN.search(key):
            result[key] = replacement
        elif isinstance(value, Mapping):
            result[key] = redact_mapping(value, replacement)
        else:
            result[key] = value
    return result
