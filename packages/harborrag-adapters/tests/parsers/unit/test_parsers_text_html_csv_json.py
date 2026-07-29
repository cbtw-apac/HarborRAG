"""White-box unit tests for the text/markdown/html/csv/json parsers."""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers.markup.engines.html.engine import HtmlMarkupEngine
from harborrag_adapters.parsers.markup.engines.markdown.engine import MarkdownMarkupEngine
from harborrag_adapters.parsers.spreadsheet.engines.csv.engine import CsvSpreadsheetEngine
from harborrag_adapters.parsers.structured.engines.json.engine import JsonStructuredEngine
from harborrag_adapters.parsers.text.engines.plain_text.engine import PlainTextEngine
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_text_parser_compacts_and_emits_paragraph():
    document = PlainTextEngine().parse(
        ParseInput(content="  hello  \n\n\n  world  \n", filename="a.txt")
    )
    assert document.parser_name == "text"
    assert document.content == "hello\n\nworld"
    assert [element.type for element in document.elements] == ["paragraph"]
    assert document.elements[0].content == "hello\n\nworld"


def test_text_parser_empty_input_has_no_elements():
    document = PlainTextEngine().parse(ParseInput(content="   \n\n", filename="a.txt"))
    assert document.content == ""
    assert document.elements == []


def test_markdown_parser_element_types_and_levels():
    markdown = "# Title\n\nBody paragraph\n\n## Sub\n\n```\ncode line\n```\n"
    document = MarkdownMarkupEngine().parse(ParseInput(content=markdown, filename="d.md"))

    types = [element.type for element in document.elements]
    assert types == ["heading", "paragraph", "heading", "code"]

    headings = [e for e in document.elements if e.type == "heading"]
    assert headings[0].content == "Title"
    assert headings[0].metadata["level"] == 1
    assert headings[1].metadata["level"] == 2

    code = next(e for e in document.elements if e.type == "code")
    assert code.content == "code line"
    # Body text is stripped of markdown markup in the flat content.
    assert "Title" in document.content
    assert "Body paragraph" in document.content
    assert document.raw == {"markdown": markdown}


def test_markdown_strips_links_and_emphasis_in_content():
    document = MarkdownMarkupEngine().parse(
        ParseInput(content="See [Harbor](https://x) and *bold*", filename="d.md")
    )
    assert "https://x" not in document.content
    assert "Harbor" in document.content
    assert "bold" in document.content
    assert "*" not in document.content


def test_html_parser_extracts_visible_text_and_strips_script_style():
    html = (
        "<html><head><style>.a{color:red}</style>"
        "<script>evil()</script></head>"
        "<body><p>Visible One</p><p>Visible Two</p></body></html>"
    )
    document = HtmlMarkupEngine().parse(ParseInput(content=html, filename="d.html"))

    assert "Visible One" in document.content
    assert "Visible Two" in document.content
    assert "evil" not in document.content
    assert "color:red" not in document.content
    # Provenance of the extraction backend is recorded honestly.
    assert document.metadata["text_engine"] == "beautifulsoup4/html.parser"
    assert [element.type for element in document.elements] == ["paragraph"]
    assert document.raw == {"html": html}


def test_csv_parser_renders_tab_separated_and_counts_rows():
    document = CsvSpreadsheetEngine().parse(
        ParseInput(content="name,role\nAda,engineer", filename="t.csv")
    )
    assert document.parser_name == "csv"
    assert document.content == "name\trole\nAda\tengineer"
    assert document.metadata["rows"] == 2
    assert [element.type for element in document.elements] == ["table"]
    assert document.elements[0].metadata["rows"] == 2


def test_csv_parser_sniffs_semicolon_dialect():
    document = CsvSpreadsheetEngine().parse(
        ParseInput(content="a;b;c\n1;2;3\n4;5;6", filename="t.csv")
    )
    # Sniffer detects ';' delimiter and re-emits as tab-separated.
    assert document.content == "a\tb\tc\n1\t2\t3\n4\t5\t6"


def test_csv_parser_handles_tsv_suffix():
    document = CsvSpreadsheetEngine().parse(ParseInput(content="a\tb\n1\t2", filename="t.tsv"))
    assert document.content == "a\tb\n1\t2"


def test_json_parser_flattens_jsonpath_lines_and_keeps_raw_json_only():
    document = JsonStructuredEngine().parse(
        ParseInput(content='{"name": "Ada", "nested": {"k": 1}}', filename="d.json")
    )
    assert "$.name: Ada" in document.content
    assert "$.nested.k: 1" in document.content
    assert document.metadata["root_type"] == "dict"
    # raw exposes the decoded json but NOT a pretty-printed duplicate.
    assert set(document.raw) == {"json"}
    assert "pretty_json" not in document.raw
    assert document.raw["json"] == {"name": "Ada", "nested": {"k": 1}}


def test_json_parser_handles_ndjson():
    document = JsonStructuredEngine().parse(
        ParseInput(content='{"a": 1}\n\n{"b": 2}\n', filename="d.ndjson")
    )
    assert document.metadata["root_type"] == "list"
    assert "$[0].a: 1" in document.content
    assert "$[1].b: 2" in document.content


def test_json_flatten_caps_at_max_depth():
    nested: dict = {}
    cursor = nested
    for _ in range(JsonStructuredEngine.MAX_FLATTEN_DEPTH + 50):
        cursor["a"] = {}
        cursor = cursor["a"]

    lines = JsonStructuredEngine._flatten(nested)
    assert any(
        f"<max-depth {JsonStructuredEngine.MAX_FLATTEN_DEPTH} reached>" in line for line in lines
    )


def test_json_flatten_renders_empty_containers():
    assert JsonStructuredEngine._flatten({}) == ["$: {}"]
    assert JsonStructuredEngine._flatten([]) == ["$: []"]
