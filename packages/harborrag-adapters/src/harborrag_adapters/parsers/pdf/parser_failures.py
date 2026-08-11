"""Failure classification for the PDF engine fallback chain."""

from __future__ import annotations

from typing import NoReturn

from harborrag_adapters.parsers.common.models import ParserAttempt
from harborrag_adapters.parsers.errors import (
    MaxFileSizeExceededError,
    MaxPagesExceededError,
    NoExtractableTextError,
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
