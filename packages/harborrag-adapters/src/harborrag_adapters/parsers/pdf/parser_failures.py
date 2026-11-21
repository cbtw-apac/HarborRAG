"""Failure classification for the PDF engine fallback chain."""

from __future__ import annotations

from typing import NoReturn

from harborrag_adapters.parsers.common.models import ParserAttempt
from harborrag_adapters.parsers.errors import (
    MaxFileSizeExceededError,
    MaxPagesExceededError,
    NoExtractableTextError,
    ParseError,
    PDFParsingFailedError,
)

TYPED_ENGINE_FAILURES = (
    MaxPagesExceededError,
    MaxFileSizeExceededError,
    NoExtractableTextError,
)


def raise_pdf_failure(attempts: list[ParserAttempt]) -> NoReturn:
    """Raise a shared typed cause, or aggregate genuinely mixed failures."""

    first_cause = attempts[0].error if attempts else None
    if first_cause is not None and all(
        type(attempt.error) is type(first_cause) for attempt in attempts
    ):
        raise first_cause
    raise PDFParsingFailedError(attempts=attempts)


def engine_failure_reason(error: Exception) -> str:
    """Return why an engine failed: a parser's own message, else only the error class.

    Another library's exception text can quote the file's path or name.
    """

    return str(error) if isinstance(error, ParseError) else type(error).__name__
