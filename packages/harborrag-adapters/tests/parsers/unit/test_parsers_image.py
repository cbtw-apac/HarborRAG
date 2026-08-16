from __future__ import annotations

import io
import logging
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from harborrag_adapters.parsers.compat import ImageParser
from harborrag_adapters.parsers.errors import ParseError
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), color="white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_rapidocr_extracts_ordered_lines_and_reuses_engine(monkeypatch) -> None:
    created: list[object] = []

    class _RapidOCR:
        def __init__(self) -> None:
            created.append(self)

        def __call__(self, _content: bytes) -> object:
            return SimpleNamespace(txts=(" first line ", "", "second line"))

    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=_RapidOCR))
    parser = ImageParser(ocr_engine="RAPIDOCR")
    parse_input = ParseInput(content=_png_bytes(), filename="scan.png")

    first = parser.parse(parse_input)
    second = parser.parse(parse_input)

    assert first.content == "first line\nsecond line"
    assert first.metadata["ocr_engine"] == "rapidocr"
    assert second.content == first.content
    assert created == [parser._rapidocr_engine]


def test_image_parser_rejects_unknown_ocr_engine() -> None:
    with pytest.raises(ValueError, match="Unsupported image OCR engine"):
        ImageParser(ocr_engine="unknown")


def test_image_parser_treats_no_ocr_text_as_empty_success(monkeypatch, caplog) -> None:
    monkeypatch.setattr(ImageParser, "_extract_text", lambda *_args: None)

    with caplog.at_level(logging.INFO, logger="harborrag.adapters.parsers.image"):
        document = ImageParser().parse(ParseInput(content=_png_bytes(), filename="blank.png"))

    assert document.content == ""
    assert document.elements == []
    assert "Parsed image OCR blank.png" in caplog.text
    assert "content_chars=0 elements=0" in caplog.text


class _TesseractError(RuntimeError):
    """Stand-in for `pytesseract.TesseractError` (status + message)."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def test_pytesseract_treats_empty_page_as_empty_success(monkeypatch) -> None:
    """Tesseract exits non-zero with an "Empty page!!" message when a page has
    no detectable text -- that used to be indistinguishable from a genuine
    failure and rewrapped as a `ParseError` instead of the empty-success path
    every other OCR engine (and this one, for a `None`/empty return) takes."""

    def _raise_empty_page(*_args: object, **_kwargs: object) -> str:
        raise _TesseractError(1, "Empty page!!\nWARNING: Invalid resolution 0 dpi.\n")

    monkeypatch.setitem(
        sys.modules,
        "pytesseract",
        SimpleNamespace(image_to_string=_raise_empty_page, TesseractError=_TesseractError),
    )

    document = ImageParser(ocr_engine="pytesseract").parse(
        ParseInput(content=_png_bytes(), filename="blank_scan.png")
    )

    assert document.content == ""
    assert document.elements == []


def test_pytesseract_other_tesseract_errors_still_raise(monkeypatch) -> None:
    """A genuine Tesseract failure (not the "no text found" signal) must keep
    surfacing as a typed `ParseError`, not be swallowed as empty success."""

    def _raise_real_failure(*_args: object, **_kwargs: object) -> str:
        raise _TesseractError(1, "Error opening data file eng.traineddata")

    monkeypatch.setitem(
        sys.modules,
        "pytesseract",
        SimpleNamespace(image_to_string=_raise_real_failure, TesseractError=_TesseractError),
    )

    with pytest.raises(ParseError, match="Image OCR failed"):
        ImageParser(ocr_engine="pytesseract").parse(
            ParseInput(content=_png_bytes(), filename="scan.png")
        )


def test_image_parser_raises_typed_error_instead_of_crashing_on_decompression_bomb(
    monkeypatch,
) -> None:
    """`Image.open()` runs its own pixel-count guard against Pillow's global
    `MAX_IMAGE_PIXELS` before our own `max_pixels` check gets a chance to run,
    raising `DecompressionBombError` (a bare `Exception`, not `OSError`). That
    used to escape uncaught instead of surfacing as a typed `ParseError`."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)

    with pytest.raises(ParseError, match="Image OCR failed"):
        ImageParser().parse(ParseInput(content=_png_bytes(), filename="huge.png"))


def test_image_parser_reports_unreadable_image_with_filename_and_size_not_raw_bytesio_repr() -> (
    None
):
    """Bytes Pillow cannot identify as any supported format (corrupt,
    truncated, or not actually an image) used to surface as a raw
    `UnidentifiedImageError: cannot identify image file <_io.BytesIO object
    at 0x...>` -- unreadable on its own, with no clue which file failed."""
    garbage = b"not an image, just plain bytes"

    with pytest.raises(ParseError) as exc_info:
        ImageParser().parse(ParseInput(content=garbage, filename="quarterly_report.png"))

    message = str(exc_info.value)
    assert "quarterly_report.png" in message
    assert str(len(garbage)) in message
    assert "_io.BytesIO" not in message


def test_image_parser_rejects_image_over_configured_max_pixels_with_clear_error() -> None:
    """A real, well-formed image whose pixel count exceeds the configured
    `max_pixels` (but stays under Pillow's own default decompression-bomb
    threshold, so `Image.open()` decodes the header fine) must be rejected
    with a message naming the offending dimensions and the limit -- not
    left to fail later as a bare `UnidentifiedImageError`."""
    width, height = 1000, 1001
    buffer = io.BytesIO()
    Image.new("L", (width, height), color=128).save(buffer, format="PNG")
    parser = ImageParser(max_pixels=1_000_000)

    with pytest.raises(
        ParseError,
        match=r"Image 1000x1001 \(1001000 pixels\) exceeds max_pixels 1000000",
    ):
        parser.parse(
            ParseInput(content=buffer.getvalue(), filename="dashboard_export_over_limit.png")
        )
