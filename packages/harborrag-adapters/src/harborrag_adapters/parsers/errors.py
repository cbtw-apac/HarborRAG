from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.ingestion import ParserRejectedDocumentError, UnsupportedDocumentError


class ParseError(ParserRejectedDocumentError, RuntimeError):
    """Raised when a parser cannot extract text from an input it owns."""


class PasswordProtectedError(ParseError):
    """Raised when an encrypted document requires a password to be parsed."""


class UnsupportedFormatError(UnsupportedDocumentError, ParseError):
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
        # Engine names alone ("Tried: pymupdf, liteparse.") hide *why* each
        # one failed. A rejection like a configured max_file_size (docling) or
        # max_pages (pymupdf) limit being exceeded produces a clear per-engine
        # `message` (e.g. "exceeding the pymupdf backend's max_pages=1 cap"),
        # but that detail lived only on `self.attempts` -- invisible in a log
        # line or an API error string. Fold it into the message itself.
        details = "; ".join(
            f"{getattr(attempt, 'engine', '<unknown>')} "
            f"({getattr(attempt, 'message', None) or 'no reason recorded'})"
            for attempt in attempts
        )
        super().__init__(f"No PDF engine produced acceptable content. Tried: {details}.")
        self.attempts = list(attempts)


class MaxPagesExceededError(ParseError):
    """Raised when a document's page count exceeds a configured engine limit.

    Distinct from :class:`PDFParsingFailedError` so a caller can quarantine
    or retry differently for a structural page-count rejection than for a
    genuine parse failure -- the two used to be indistinguishable once
    folded into the router's generic aggregate message.
    """

    def __init__(self, *, page_count: int, max_pages: int, engine: str) -> None:
        super().__init__(
            f"Document has {page_count} pages, exceeds configured max_num_pages "
            f"{max_pages} ({engine} backend)."
        )
        self.page_count = page_count
        self.max_pages = max_pages
        self.engine = engine


class MaxFileSizeExceededError(ParseError):
    """Raised when a document's size exceeds a configured engine limit.

    See :class:`MaxPagesExceededError` for why this stays a distinct type
    instead of a generic :class:`ParseError` string.
    """

    def __init__(self, *, size_bytes: int, max_bytes: int, engine: str) -> None:
        super().__init__(
            f"File size {size_bytes} bytes exceeds configured max_file_size "
            f"{max_bytes} bytes ({engine} backend)."
        )
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        self.engine = engine


class NoExtractableTextError(ParseError):
    """Raised when a document has pages but no page yielded a text layer.

    Distinguishes an image-only/scanned document (which may need OCR) from a
    genuinely empty or corrupt file, so callers can route or quarantine it
    accordingly instead of receiving the same generic failure as any other
    rejection.
    """

    def __init__(self, *, page_count: int) -> None:
        super().__init__(
            f"Document has {page_count} page(s) but no extractable text layer "
            "was found; this may be a scanned/image-only document that requires OCR."
        )
        self.page_count = page_count


class EncryptedPdfError(ParseError):
    """Raised when a PDF is password-protected and cannot be decoded.

    This terminates a PDF backend fallback chain: no downstream engine can
    extract text from an encrypted document, so retrying wastes OCR budget.
    """


class TextDecodingError(ParseError):
    """Raised when input bytes cannot be confidently decoded as text.

    Statistical encoding detection (e.g. charset_normalizer) can report a
    low-risk guess for byte sequences that are actually corrupted UTF-8 with
    only a handful of invalid bytes: single-byte legacy codepages accept
    almost any byte value, so a few bad bytes surrounded by ASCII text get
    "confidently" reinterpreted as unrelated characters (commonly Cyrillic
    under cp1251) instead of surfacing the corruption.
    """

    def __init__(self, *, byte_length: int, guessed_encoding: str | None = None) -> None:
        detail = (
            f" (best guess {guessed_encoding!r} was not trustworthy)" if guessed_encoding else ""
        )
        super().__init__(f"Could not confidently decode {byte_length} bytes as text{detail}.")
        self.byte_length = byte_length
        self.guessed_encoding = guessed_encoding
