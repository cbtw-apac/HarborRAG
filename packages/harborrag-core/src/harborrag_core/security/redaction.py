from __future__ import annotations

import re

# The unquoted branch stops before either a delimiter (",", ";", "&", "}", "]",
# a line break) or the start of the next "key[:=]" pair, so a multi-word secret
# ("password: My Secret Passphrase 123") is redacted in full while an adjacent
# field on the same line ("api_key=abc123&user=alice") survives.
_UNQUOTED_VALUE = r"(?:(?!\s+[\w.-]+\s*[:=]).)+?(?=[,;&}\]\r\n]|\s+[\w.-]+\s*[:=]|$)"
_VALUE = rf'(?:"[^"]*"|\'[^\']*\'|{_UNQUOTED_VALUE})'

_LABELED_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?key|token|secret|password|credential)"
        r"[\w-]*['\"]?\s*[:=]\s*" + _VALUE
    ),
    re.compile(r"(?i)(authorization)['\"]?\s*:\s*['\"]?\s*bearer\s+" r"([^\s,'\";}\]]+)"),
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
