from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from harborrag_adapters.parsers.compat import ImageParser
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
