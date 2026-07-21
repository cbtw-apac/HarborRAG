"""Archive decompression bomb hardening tests."""

from __future__ import annotations

import pytest
from harbor_test_builders import build_zip_bomb_bytes
from harborrag_adapters.parsers import HarborParser
from harborrag_adapters.parsers.exceptions import ParseError
from harborrag_adapters.parsers.utils import open_guarded_zip
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.blackbox


def test_open_guarded_zip_rejects_compression_bomb() -> None:
    with pytest.raises(ParseError, match="ratio|uncompressed|members"):
        open_guarded_zip(build_zip_bomb_bytes())


def test_epub_parser_rejects_bomb_via_public_api() -> None:
    parser = HarborParser()
    with pytest.raises(ParseError):
        parser.parse(ParseInput(content=build_zip_bomb_bytes(), filename="b.epub"))
