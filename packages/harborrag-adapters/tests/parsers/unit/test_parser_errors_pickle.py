"""Every parser error survives a pickle round trip with its type and attributes.

An isolated parse returns the parser's own exception from a child process, so
an error that pickles but cannot be rebuilt (keyword-only ``__init__``) would
reach the caller as a bare ``TypeError`` and lose its classification.
"""

from __future__ import annotations

import inspect
import pickle

import pytest

from harborrag_adapters.parsers import errors
from harborrag_adapters.parsers.common.models import ParserAttempt

_SAMPLES: dict[type[BaseException], BaseException] = {
    errors.ParseError: errors.ParseError("bad input"),
    errors.PasswordProtectedError: errors.PasswordProtectedError("locked"),
    errors.UnsupportedFormatError: errors.UnsupportedFormatError("no parser"),
    errors.UnsupportedParserError: errors.UnsupportedParserError(
        filename="notes.xyz", mime_type="application/x-example"
    ),
    errors.UnsupportedParserEngineError: errors.UnsupportedParserEngineError(
        family="pdf", engine="engine-a"
    ),
    errors.ParserDependencyError: errors.ParserDependencyError(engine="engine-a", extra="ocr"),
    errors.DuplicatePDFEngineError: errors.DuplicatePDFEngineError("engine-a"),
    errors.UnknownPDFEngineError: errors.UnknownPDFEngineError("engine-a"),
    errors.PDFParsingFailedError: errors.PDFParsingFailedError(
        attempts=[ParserAttempt(engine="engine-a", success=False, duration_ms=1.0, message="no")]
    ),
    errors.MaxPagesExceededError: errors.MaxPagesExceededError(
        page_count=9, max_pages=1, engine="engine-a"
    ),
    errors.MaxFileSizeExceededError: errors.MaxFileSizeExceededError(
        size_bytes=9, max_bytes=1, engine="engine-a"
    ),
    errors.NoExtractableTextError: errors.NoExtractableTextError(page_count=2),
    errors.MaxPixelsExceededError: errors.MaxPixelsExceededError(
        width=4, height=4, max_pixels=1, filename="image.png"
    ),
    errors.UnreadableImageError: errors.UnreadableImageError(filename="image.png", size_bytes=3),
    errors.EncryptedPdfError: errors.EncryptedPdfError("encrypted"),
    errors.TextDecodingError: errors.TextDecodingError(byte_length=5, guessed_encoding="cp1251"),
}


def test_every_error_class_in_the_module_has_a_sample() -> None:
    defined = {
        cls
        for _, cls in inspect.getmembers(errors, inspect.isclass)
        if issubclass(cls, BaseException) and cls.__module__ == errors.__name__
    }

    assert defined == set(_SAMPLES)


@pytest.mark.parametrize("error", list(_SAMPLES.values()), ids=lambda e: type(e).__name__)
def test_the_error_round_trips_with_its_type_and_attributes(error: BaseException) -> None:
    restored = pickle.loads(pickle.dumps(error))

    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert restored.args == error.args
    assert vars(restored) == vars(error)
