from __future__ import annotations

from collections.abc import Sequence


class ParseError(RuntimeError):
    """Raised when a parser cannot extract text from an input it owns."""


class UnsupportedFormatError(ParseError):
    """Raised when no registered parser supports an input."""


class UnsupportedParserError(UnsupportedFormatError):
    """Raised when no parser family owns a filename or MIME type."""

    def __init__(self, *, filename: str | None, mime_type: str | None) -> None:
        super().__init__(
            f"No parser family registered for filename={filename!r} mime_type={mime_type!r}."
        )
        self.filename = filename
        self.mime_type = mime_type


class UnsupportedParserEngineError(ParseError):
    """Raised when a family cannot resolve a requested provider engine."""

    def __init__(self, *, family: str, engine: str) -> None:
        super().__init__(f"Unknown {family} parser engine: {engine!r}")
        self.family = family
        self.engine = engine


class ParserDependencyError(ParseError):
    """Raised when an optional provider dependency is unavailable."""

    def __init__(self, *, engine: str, extra: str) -> None:
        super().__init__(
            f"Parser engine {engine!r} is unavailable; install `harborrag-adapters[{extra}]`."
        )
        self.engine = engine
        self.extra = extra


class DuplicatePDFEngineError(ParseError):
    """Raised when a PDF engine name is registered more than once."""

    def __init__(self, engine: str) -> None:
        super().__init__(f"PDF engine {engine!r} is already registered.")
        self.engine = engine


class UnknownPDFEngineError(UnsupportedParserEngineError):
    """Raised when PDF routing references an unregistered engine."""

    def __init__(self, engine: str) -> None:
        super().__init__(family="pdf", engine=engine)


class PDFParsingFailedError(ParseError):
    """Raised after every configured PDF engine attempt is rejected."""

    def __init__(self, *, attempts: Sequence[object]) -> None:
        names = ", ".join(str(getattr(attempt, "engine", "<unknown>")) for attempt in attempts)
        super().__init__(f"No PDF engine produced acceptable content. Tried: {names}.")
        self.attempts = list(attempts)


class EncryptedPdfError(ParseError):
    """Raised when a PDF is password-protected and cannot be decoded.

    This terminates a PDF backend fallback chain: no downstream engine can
    extract text from an encrypted document, so retrying wastes OCR budget.
    """
