"""White-box unit tests for BaseParser routing (can_parse, __init_subclass__)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from harborrag_adapters.parsers import (
    CsvParser,
    DocxParser,
    EpubParser,
    ExcelParser,
    HtmlParser,
    ImageParser,
    JsonParser,
    MarkdownParser,
    PptxParser,
    TextParser,
)
from harborrag_adapters.parsers.base import BaseParser
from harborrag_adapters.parsers.utils import normalize_suffix
from harborrag_core.domain.parser import ParsedDocument, ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    ("parser", "filename", "content_type"),
    [
        (TextParser(), "notes.txt", None),
        (TextParser(), None, "text/plain"),
        (MarkdownParser(), "doc.md", None),
        (MarkdownParser(), None, "text/markdown"),
        (HtmlParser(), "page.html", None),
        (HtmlParser(), None, "text/html"),
        (CsvParser(), "table.csv", None),
        (CsvParser(), "table.tsv", None),
        (CsvParser(), None, "text/csv"),
        (JsonParser(), "data.json", None),
        (JsonParser(), None, "application/json"),
        (DocxParser(), "report.docx", None),
        (PptxParser(), "deck.pptx", None),
        (ExcelParser(), "sheet.xlsx", None),
        (ImageParser(), "pic.png", None),
        (ImageParser(), None, "image/png"),
        (EpubParser(), "book.epub", None),
    ],
)
def test_can_parse_matches_advertised_routes(parser, filename, content_type):
    assert parser.can_parse(ParseInput(content=b"x", filename=filename, content_type=content_type))


@pytest.mark.parametrize(
    "parser",
    [MarkdownParser(), CsvParser(), JsonParser(), DocxParser(), ImageParser()],
)
def test_can_parse_rejects_unrelated_input(parser):
    assert not parser.can_parse(
        ParseInput(content=b"x", filename="mystery.zzz", content_type="x/y")
    )


def test_can_parse_uses_normalized_content_type_parameters():
    # Content type with parameters is normalized before comparison.
    assert HtmlParser().can_parse(ParseInput(content=b"x", content_type="text/html; charset=utf-8"))


def test_init_subclass_normalizes_suffix_and_content_type_declarations():
    class WeirdParser(BaseParser[ParseInput, ParsedDocument]):
        parser_name: ClassVar[str] = "weird"
        # Mixed case, missing dots, surrounding whitespace, and an empty entry.
        suffixes: ClassVar[frozenset[str]] = frozenset({"FOO", ".Bar", " baz "})
        content_types: ClassVar[frozenset[str]] = frozenset(
            {"Application/X-Weird", "  ", "TEXT/Thing "}
        )

        def parse(self, input: ParseInput) -> ParsedDocument:  # pragma: no cover
            raise NotImplementedError

    assert WeirdParser.suffixes == frozenset({".foo", ".bar", ".baz"})
    assert WeirdParser.content_types == frozenset({"application/x-weird", "text/thing"})


def test_normalize_suffix_helper():
    assert normalize_suffix("TXT") == ".txt"
    assert normalize_suffix(".MD") == ".md"
    assert normalize_suffix("  Json ") == ".json"
    assert normalize_suffix("") == ""
