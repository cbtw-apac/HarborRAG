"""Archive decompression bomb hardening tests."""

from __future__ import annotations

import io
import zipfile

import pytest
from harbor_test_builders import (
    build_understated_file_size_zip_bytes,
    build_zero_compressed_size_zip_bytes,
    build_zip_bomb_bytes,
)

from harborrag_adapters.parsers import HarborParserFactory
from harborrag_adapters.parsers.common.validation import open_guarded_zip
from harborrag_adapters.parsers.errors import ParseError
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.blackbox


def test_open_guarded_zip_rejects_compression_bomb() -> None:
    with pytest.raises(ParseError, match="ratio|uncompressed|members"):
        open_guarded_zip(build_zip_bomb_bytes())


def test_open_guarded_zip_rejects_zero_compressed_size_with_nonzero_file_size() -> None:
    """A forged member claiming 0 compressed bytes for a non-zero
    uncompressed size is an effectively infinite ratio and must be rejected
    outright, distinct from the compression-ratio bomb covered above."""
    with pytest.raises(ParseError, match="0 compressed bytes"):
        open_guarded_zip(build_zero_compressed_size_zip_bytes())


def test_open_guarded_zip_forged_file_size_cannot_smuggle_more_real_bytes() -> None:
    """A central directory that under-reports a member's uncompressed size
    is not a viable bypass: CPython's zipfile truncates decompressed output
    to the declared (possibly forged) file_size and then fails its own CRC
    check, so no consumer reading through the standard zipfile API --
    openpyxl, python-pptx, or this guard -- can ever recover more real bytes
    than the metadata claims."""
    real_content = b"x" * 500
    forged = build_understated_file_size_zip_bytes(real_content, claimed_file_size=10)

    with zipfile.ZipFile(io.BytesIO(forged)) as archive:
        with pytest.raises(zipfile.BadZipFile, match="Bad CRC-32"):
            archive.read(archive.infolist()[0].filename)


def test_pptx_parser_rejects_zero_compressed_size_bomb_via_public_api() -> None:
    parser = HarborParserFactory().create_registry()
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=build_zero_compressed_size_zip_bytes(), filename="b.pptx"))


def test_xlsx_parser_rejects_zero_compressed_size_bomb_via_public_api() -> None:
    parser = HarborParserFactory().create_registry()
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=build_zero_compressed_size_zip_bytes(), filename="b.xlsx"))


def test_epub_parser_rejects_bomb_via_public_api() -> None:
    parser = HarborParserFactory().create_registry()
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=build_zip_bomb_bytes(), filename="b.epub"))


def test_pptx_parser_rejects_bomb_via_public_api() -> None:
    # PPTX is a zip container like DOCX/EPUB; the guard must run before bytes
    # reach python-pptx, not just when called directly (regression coverage
    # for the guard being skipped in PythonPptxPresentationEngine.parse()).
    parser = HarborParserFactory().create_registry()
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=build_zip_bomb_bytes(), filename="b.pptx"))


def test_xlsx_parser_rejects_bomb_via_public_api() -> None:
    # XLSX is a zip container like DOCX/EPUB; the guard must run before bytes
    # reach openpyxl, not just when called directly (regression coverage for
    # the guard being skipped in ExcelSpreadsheetEngine._parse_openxml()).
    parser = HarborParserFactory().create_registry()
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=build_zip_bomb_bytes(), filename="b.xlsx"))
