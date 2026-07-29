"""Blackbox smoke tests routing sample inputs through the default parser stack."""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers import HarborParserFactory
from harborrag_adapters.parsers.markup.engines.html.engine import HtmlMarkupEngine
from harborrag_adapters.parsers.markup.engines.markdown.engine import MarkdownMarkupEngine
from harborrag_adapters.parsers.spreadsheet.engines.csv.engine import CsvSpreadsheetEngine
from harborrag_adapters.parsers.structured.engines.json.engine import JsonStructuredEngine
from harborrag_adapters.parsers.text.engines.plain_text.engine import PlainTextEngine
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit


@pytest.mark.blackbox
@pytest.mark.parametrize(
    ("parse_input", "expected_parser", "expected_content"),
    [
        (
            ParseInput(content="name,role\nAda,engineer", content_type="text/csv"),
            CsvSpreadsheetEngine.parser_name,
            "Ada\tengineer",
        ),
        (
            ParseInput(content='{"name": "Ada"}', filename="data.json"),
            JsonStructuredEngine.parser_name,
            "$.name: Ada",
        ),
        (
            ParseInput(content="# Title\n\nBody", filename="doc.md"),
            MarkdownMarkupEngine.parser_name,
            "Title\n\nBody",
        ),
        (
            ParseInput(content="print('hello')", filename="app.py"),
            PlainTextEngine.parser_name,
            "print('hello')",
        ),
        (
            ParseInput(content="plain text", content_type="text/plain"),
            PlainTextEngine.parser_name,
            "plain text",
        ),
        (
            ParseInput(
                content="<html><script>x()</script><p>Hello</p></html>",
                filename="doc.html",
            ),
            HtmlMarkupEngine.parser_name,
            "Hello",
        ),
    ],
)
def test_builtin_text_parsers_blackbox(parse_input, expected_parser, expected_content):
    document = HarborParserFactory().create_registry().parse(parse_input)

    assert document.parser_name == expected_parser
    assert expected_content in document.content
    assert document.elements
    assert all(element.content for element in document.elements)
