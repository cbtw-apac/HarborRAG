"""Unit tests for the LiteParse PDF backend."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harborrag_adapters.parsers import LiteParseBackend, LiteParseBackendOptions
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit


class FakeLiteParse:
    def __init__(self) -> None:
        self.input: str | bytes | None = None

    def parse(self, input: str | bytes) -> SimpleNamespace:
        self.input = input
        return SimpleNamespace(
            text="Hello\nWorld",
            pages=[
                {
                    "page_num": 1,
                    "text_items": [{"text": "Hello"}, {"text": "World"}],
                }
            ],
        )


@pytest.mark.whitebox
def test_liteparse_backend_uses_llamaindex_liteparse_api_shape():
    fake_parser = FakeLiteParse()

    backend = LiteParseBackend(
        LiteParseBackendOptions(
            parser=fake_parser,
            output_format="markdown",
            ocr_enabled=False,
            target_pages="1-2",
        )
    )

    document = backend.parse(ParseInput(content=b"%PDF", filename="report.pdf"))

    assert Path(str(fake_parser.input)).name == "document.pdf"
    assert document.content == "Hello\nWorld"
    assert document.metadata["liteparse_output_format"] == "markdown"
    assert document.metadata["liteparse_ocr_enabled"] is False
    assert document.metadata["liteparse_target_pages"] == "1-2"
    assert document.metadata["page_count"] == 1
    assert document.elements[0].content == "Hello\nWorld"
    assert document.elements[0].metadata["page"] == 1


@pytest.mark.whitebox
def test_liteparse_backend_treats_empty_text_as_empty_not_missing():
    class _FakeEmptyLiteParse:
        def parse(self, _input: str | bytes) -> SimpleNamespace:
            # `text == ""` is a genuine empty extraction; it must not be
            # treated the same as `text is None` and fall back to dumping
            # the whole result object as content.
            return SimpleNamespace(text="", pages=[])

    backend = LiteParseBackend(LiteParseBackendOptions(parser=_FakeEmptyLiteParse()))

    document = backend.parse(ParseInput(content=b"%PDF", filename="empty.pdf"))

    assert document.content == ""


@pytest.mark.whitebox
def test_liteparse_constructor_kwargs_match_documented_python_options():
    backend = LiteParseBackend(
        LiteParseBackendOptions(
            output_format="markdown",
            image_mode="off",
            extract_links=False,
            ocr_enabled=False,
            ocr_language="fra",
            target_pages="1-5",
            dpi=300,
            extra_options={"custom_option": "value"},
        )
    )

    assert backend._constructor_kwargs() == {
        "output_format": "markdown",
        "image_mode": "off",
        "extract_links": False,
        "ocr_enabled": False,
        "ocr_language": "fra",
        "target_pages": "1-5",
        "dpi": 300,
        "custom_option": "value",
    }
