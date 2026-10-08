"""Redact credentials from free text and from mappings, by one shared definition."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .field_names import canonical_field_name, is_sensitive_field_name

# Header names whose *value* is a credential although the name does not say so.
# Whole-name only: ``cookie_domain`` is configuration, ``Cookie`` is a session.
_CREDENTIAL_HEADER_NAMES = frozenset({"cookie", "set_cookie"})


def is_sensitive_label(label: object) -> bool:
    """Whether a mapping key or a free-text label names a credential.

    The one key test for both redaction paths, built on ``is_sensitive_field_name``
    so configuration validation, error sanitisation and redaction cannot drift.
    """

    return is_sensitive_field_name(label) or canonical_field_name(label) in _CREDENTIAL_HEADER_NAMES


# The unquoted branch stops before either a delimiter (",", ";", "&", "}", "]",
# a line break) or the start of the next "key[:=]" pair, so a multi-word secret
# ("password: My Secret Passphrase 123") is redacted in full while an adjacent
# field on the same line ("api_key=abc123&user=alice") survives.
# The ``(?<!\s)`` anchors keep a whitespace run from being rescanned from every
# position inside it (quadratic on a long run of blanks): only its start counts.
_UNQUOTED_VALUE = r"(?:(?!(?<!\s)\s+[\w.-]+\s*[:=]).)+?(?=[,;&}\]\r\n]|(?<!\s)\s+[\w.-]+\s*[:=]|$)"
_VALUE = rf'(?:"[^"]*"|\'[^\']*\'|{_UNQUOTED_VALUE})'

# Authorization carries a scheme before the credential ("Bearer x", "Basic y"),
# and the generic value branch below stops at what looks like the next "key="
# pair -- which base64 padding does. So it gets its own branch that consumes to
# a real delimiter, placed first.
_AUTHORIZATION_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^,;&}\]\r\n]+)"
# A cookie header is "name=value; name=value": every pair is session state.
# Quotes do not end it: ``Cookie: "a=b"; other=c`` must not leak ``other=c``.
_COOKIE_VALUE = r"[^\r\n]+"

_LABELED_PATTERNS = [
    re.compile(r"(?i)(authorization)['\"]?\s*[:=]\s*" + _AUTHORIZATION_VALUE),
    re.compile(r"(?i)\b((?:set-)?cookie)['\"]?\s*:\s*" + _COOKIE_VALUE),
]

# Any whole word followed by a key separator; ``is_sensitive_label`` decides whether
# it names a credential. Anchored at a word start and matched possessively, so a long
# word is scanned once rather than from every credential stem inside it, and only the
# label is consumed: what follows a harmless label is still scanned for credentials.
_CANDIDATE_LABEL = re.compile(r"(?<![\w-])([\w-]++)['\"]?\s*+[:=]\s*+")
_LABELED_VALUE = re.compile(_VALUE)

# Credentials with no label of their own: the affixes are kept, the secret is not.
_AFFIXED_PATTERNS = [
    # "curl -H 'Bearer eyJ...'": an unlabeled bearer credential.
    # Token-like content only (a digit or one of ``._~+/=-``), so prose such as
    # "Bearer authentication is required" is left alone.
    re.compile(
        r"(?i)\b(?P<prefix>bearer\s+)(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=-])[A-Za-z0-9._~+/=-]{8,}"
    ),
    # URL userinfo: "https://user:pass@host" or "https://token@host".
    # The lookbehind keeps a scheme from restarting inside a run ("a.a.a."),
    # which made the scan quadratic; ``?`` and ``#`` end the authority.
    re.compile(r"(?i)(?<![a-z0-9+.-])(?P<prefix>[a-z][a-z0-9+.-]*://)[^\s/@?#]+(?P<suffix>@)"),
    # Signed-URL query credentials: Azure SAS, SharePoint, S3 and GCS signatures.
    re.compile(
        r"(?i)(?P<prefix>[?&](?:sig|signature|tempauth|x-amz-signature"
        r"|x-amz-security-token|x-goog-signature)=)[^&#\s]+"
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


def _labeled(match: re.Match[str], replacement: str) -> str:
    return f"{match.group(1)}={replacement}"


def _redact_labeled_values(text: str, replacement: str) -> str:
    """Mask the value of every credential label, in one left-to-right pass."""

    parts: list[str] = []
    position = 0
    while (label := _CANDIDATE_LABEL.search(text, position)) is not None:
        value = None
        if is_sensitive_label(label.group(1)):
            value = _LABELED_VALUE.match(text, label.end())
        if value is None:
            parts.append(text[position : label.end()])
            position = label.end()
            continue
        parts.append(text[position : label.start()])
        parts.append(f"{label.group(1)}={replacement}")
        position = value.end()
    parts.append(text[position:])
    return "".join(parts)


def _affixed(match: re.Match[str], replacement: str) -> str:
    return f"{match.group('prefix')}{replacement}{match.groupdict().get('suffix') or ''}"


def redact_secrets(text: str, replacement: str = "<redacted>") -> str:
    result = text
    for pattern in _LABELED_PATTERNS:
        result = pattern.sub(lambda match: _labeled(match, replacement), result)
    result = _redact_labeled_values(result, replacement)
    for pattern in _AFFIXED_PATTERNS:
        result = pattern.sub(lambda match: _affixed(match, replacement), result)
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
    """Recursively mask values whose key names a credential.

    Defense-in-depth for DTO boundaries that serialize free-form config
    (e.g. SourceConfig.config): a key ``is_sensitive_label`` accepts is masked
    regardless of what invariants upstream write-side code is supposed to
    enforce. Mappings nested inside lists/tuples are recursed into, and string
    values are additionally passed through redact_secrets() to catch secrets
    embedded in free-form text (e.g. a header line under a non-sensitive key).
    """

    result: dict[str, Any] = {}
    for key, value in data.items():
        if is_sensitive_label(key):
            result[key] = replacement
        else:
            result[key] = _redact_value(value, replacement)
    return result
