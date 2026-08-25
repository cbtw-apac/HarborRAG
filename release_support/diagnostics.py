"""Redaction helpers for operator-facing release diagnostics."""

import re

_REDACTIONS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|token)\s+)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+)\b"), "[REDACTED]"),
    (
        re.compile(r"(?i)([?&](?:access_?token|api_?key|token|secret)=)[^&\s]+"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@"), r"\1[REDACTED]@"),
)


def redact_diagnostic(value: object) -> str:
    """Remove common credentials from subprocess and HTTP diagnostic text."""

    redacted = str(value)
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
