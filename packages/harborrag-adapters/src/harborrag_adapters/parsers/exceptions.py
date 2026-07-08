from __future__ import annotations


class ParseError(RuntimeError):
    """Raised when a parser cannot extract text from an input it owns."""


class UnsupportedFormatError(ParseError):
    """Raised when no registered parser supports an input."""

