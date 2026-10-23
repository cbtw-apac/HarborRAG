"""Operator-facing release output: log formatting and credential redaction."""

import logging
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


class CleanFormatter(logging.Formatter):
    """Compact formatter for an operator-facing release command."""

    PREFIXES = {
        logging.DEBUG: "🔍 ",
        logging.WARNING: "⚠️  ",
        logging.ERROR: "❌ ",
    }

    def format(self, record: logging.LogRecord) -> str:
        return f"{self.PREFIXES.get(record.levelno, '')}{record.getMessage()}"


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the release logger."""

    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(CleanFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logger = logging.getLogger("release")
    logger.setLevel(level)
    return logger
