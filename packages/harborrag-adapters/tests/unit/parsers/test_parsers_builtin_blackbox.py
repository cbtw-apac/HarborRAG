"""Blackbox smoke tests routing sample inputs through the default parser stack."""

from __future__ import annotations

import pytest
from harborrag_adapters.parsers import (
    CsvParser,
    HarborParser,
    HtmlParser,
    JsonParser,
    MarkdownParser,
    TextParser,
)
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit


@pytest.mark.blackbox
@pytest.mark.parametrize(
    ("parse_input", "expected_parser", "expected_content"),
    [
        (
            ParseInput(content="name,role\nAda,engineer", content_type="text/csv"),
            CsvParser.parser_name,
            "Ada\tengineer",
        ),
        (
            ParseInput(content='{"name": "Ada"}', filename="data.json"),
            JsonParser.parser_name,
            "$.name: Ada",
        ),
        (
            ParseInput(content="# Title\n\nBody", filename="doc.md"),
            MarkdownParser.parser_name,
            "Title\n\nBody",
        ),
        (
            ParseInput(content="print('hello')", filename="app.py"),
            TextParser.parser_name,
            "print('hello')",
        ),
        (
            ParseInput(content="plain text", content_type="text/plain"),
            TextParser.parser_name,
            "plain text",
        ),
        (
            ParseInput(
                content="<html><script>x()</script><p>Hello</p></html>",
                filename="doc.html",
            ),
            HtmlParser.parser_name,
            "Hello",
        ),
    ],
)
def test_builtin_text_parsers_blackbox(parse_input, expected_parser, expected_content):
    document = HarborParser().parse(parse_input)

    assert document.parser_name == expected_parser
    assert expected_content in document.content
    assert document.elements
    assert all(element.content for element in document.elements)
