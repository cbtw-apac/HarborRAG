from __future__ import annotations


class ParseError(RuntimeError):
    """Raised when a parser cannot extract text from an input it owns."""


class UnsupportedFormatError(ParseError):
    """Raised when no registered parser supports an input."""


class EncryptedPdfError(ParseError):
    """Raised when a PDF is password-protected and cannot be decoded.

    This terminates a PDF backend fallback chain: no downstream engine can
    extract text from an encrypted document, so retrying wastes OCR budget.
    """
