"""Failure and recovery tests for parser adapters.

The contract under test: corrupt, hostile, or unsupported inputs surface as the
documented :class:`ParseError` (or its :class:`UnsupportedFormatError` subclass)
rather than leaking raw library exceptions such as ``BadZipFile``, ``KeyError``,
``csv.Error``, ``json.JSONDecodeError``, or ``RecursionError``.
"""
from __future__ import annotations

import csv

import pytest

from harbor_test_builders import build_zip_bomb_bytes
from harborrag_adapters.parsers import (
    DocxParser,
    EpubParser,
    ExcelParser,
    HarborParser,
    ImageParser,
    JsonParser,
    PptxParser,
)
from harborrag_adapters.parsers.exceptions import ParseError, UnsupportedFormatError
from harborrag_adapters.parsers.utils import open_guarded_zip
from harborrag_core.domain.parser import ParseInput


pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Corrupt zip-container office/ebook documents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parser", "filename"),
    [
        (DocxParser(), "broken.docx"),
        (PptxParser(), "broken.pptx"),
        (ExcelParser(), "broken.xlsx"),
        (EpubParser(), "broken.epub"),
    ],
)
def test_non_zip_bytes_raise_parse_error(parser, filename):
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=b"not a zip file at all", filename=filename))


@pytest.mark.parametrize(
    ("parser", "filename"),
    [
        (DocxParser(), "trunc.docx"),
        (PptxParser(), "trunc.pptx"),
        (ExcelParser(), "trunc.xlsx"),
        (EpubParser(), "trunc.epub"),
    ],
)
def test_truncated_zip_local_header_raises_parse_error(parser, filename):
    # A valid ZIP local file signature followed by garbage: enough to look like
    # a zip but impossible to read cleanly.
    truncated = b"PK\x03\x04" + b"\x00" * 8 + b"corrupt-and-incomplete"
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=truncated, filename=filename))


def test_corrupt_docx_does_not_leak_raw_badzipfile():
    import zipfile

    with pytest.raises(ParseError) as excinfo:
        DocxParser().parse(ParseInput(content=b"not a zip", filename="x.docx"))
    # ParseError is raised, and the original library error is preserved as cause
    # rather than propagating uncaught.
    assert not isinstance(excinfo.value, zipfile.BadZipFile)


# ---------------------------------------------------------------------------
# JSON failures
# ---------------------------------------------------------------------------


def test_invalid_json_raises_parse_error():
    with pytest.raises(ParseError, match="Invalid JSON"):
        JsonParser().parse(
            ParseInput(content='{"unterminated": ', filename="bad.json")
        )


def test_deeply_nested_json_raises_parse_error():
    # Adversarial nesting: json.loads raises RecursionError or JSONDecodeError,
    # both normalized to ParseError.
    with pytest.raises(ParseError):
        JsonParser().parse(ParseInput(content="[" * 10000, filename="bomb.json"))


def test_invalid_ndjson_line_raises_parse_error():
    with pytest.raises(ParseError):
        JsonParser().parse(
            ParseInput(content='{"ok": 1}\nnot-json-here', filename="bad.ndjson")
        )


# ---------------------------------------------------------------------------
# CSV oversized field
# ---------------------------------------------------------------------------


def test_csv_oversized_field_raises_parse_error():
    from harborrag_adapters.parsers.structured import CsvParser

    original_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(16)
        oversized = "x" * 5000
        with pytest.raises(ParseError, match="Invalid CSV"):
            CsvParser().parse(
                ParseInput(content=f"col\n{oversized}", filename="big.csv")
            )
    finally:
        csv.field_size_limit(original_limit)


# ---------------------------------------------------------------------------
# Decompression bombs
# ---------------------------------------------------------------------------


def test_open_guarded_zip_rejects_zip_bomb():
    with pytest.raises(ParseError):
        open_guarded_zip(build_zip_bomb_bytes())


def test_epub_parse_of_zip_bomb_raises_parse_error(zip_bomb_bytes):
    with pytest.raises(ParseError):
        EpubParser().parse(ParseInput(content=zip_bomb_bytes, filename="bomb.epub"))


# ---------------------------------------------------------------------------
# Corrupt image (PIL is installed)
# ---------------------------------------------------------------------------


def test_corrupt_image_bytes_raise_parse_error():
    with pytest.raises(ParseError):
        ImageParser().parse(
            ParseInput(content=b"this is not a valid image", filename="broken.png")
        )


# ---------------------------------------------------------------------------
# Unsupported format routing
# ---------------------------------------------------------------------------


def test_unknown_suffix_and_content_type_raise_unsupported_format():
    registry = HarborParser()
    with pytest.raises(UnsupportedFormatError, match="No parser registered"):
        registry.parse(
            ParseInput(
                content=b"\x00\x01\x02",
                filename="mystery.zzz",
                content_type="application/x-unknown",
            )
        )


def test_unsupported_format_is_a_parse_error_subclass():
    # parse_many(on_error="skip") relies on this to quarantine unroutable inputs.
    assert issubclass(UnsupportedFormatError, ParseError)
