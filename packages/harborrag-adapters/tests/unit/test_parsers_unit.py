"""White-box unit tests for internal parser logic.

These exercise routing, per-parser extraction, metadata provenance, and the
shared parser utilities directly, without going through any network or optional
heavyweight backends (docling/mineru/paddleocr are never touched here).
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from harbor_test_builders import (
    build_epub_bytes,
    build_pptx_bytes,
    build_xlsx_bytes,
)
from harborrag_adapters.parsers import (
    CsvParser,
    DocxParser,
    EpubParser,
    ExcelParser,
    HarborParser,
    HtmlParser,
    ImageParser,
    JsonParser,
    MarkdownParser,
    PptxParser,
    TextParser,
)
from harborrag_adapters.parsers.base import BaseParser
from harborrag_adapters.parsers.exceptions import ParseError, UnsupportedFormatError
from harborrag_adapters.parsers.utils import (
    DEFAULT_MAX_INPUT_BYTES,
    compact_text,
    guard_input_size,
    html_to_text_with_engine,
    normalize_suffix,
    parse_metadata,
)
from harborrag_core.domain.parser import ParsedDocument, ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


# ---------------------------------------------------------------------------
# Routing: can_parse, suffix/content-type advertisement, __init_subclass__
# ---------------------------------------------------------------------------


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
    assert parser.can_parse(
        ParseInput(content=b"x", filename=filename, content_type=content_type)
    )


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
    assert HtmlParser().can_parse(
        ParseInput(content=b"x", content_type="text/html; charset=utf-8")
    )


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


# ---------------------------------------------------------------------------
# TextParser
# ---------------------------------------------------------------------------


def test_text_parser_compacts_and_emits_paragraph():
    document = TextParser().parse(
        ParseInput(content="  hello  \n\n\n  world  \n", filename="a.txt")
    )
    assert document.parser_name == "text"
    assert document.content == "hello\n\nworld"
    assert [element.type for element in document.elements] == ["paragraph"]
    assert document.elements[0].content == "hello\n\nworld"


def test_text_parser_empty_input_has_no_elements():
    document = TextParser().parse(ParseInput(content="   \n\n", filename="a.txt"))
    assert document.content == ""
    assert document.elements == []


# ---------------------------------------------------------------------------
# MarkdownParser
# ---------------------------------------------------------------------------


def test_markdown_parser_element_types_and_levels():
    markdown = "# Title\n\nBody paragraph\n\n## Sub\n\n```\ncode line\n```\n"
    document = MarkdownParser().parse(ParseInput(content=markdown, filename="d.md"))

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


def test_markdown_strips_links_and_emphasis_in_content():
    document = MarkdownParser().parse(
        ParseInput(content="See [Harbor](https://x) and *bold*", filename="d.md")
    )
    assert "https://x" not in document.content
    assert "Harbor" in document.content
    assert "bold" in document.content
    assert "*" not in document.content


# ---------------------------------------------------------------------------
# HtmlParser
# ---------------------------------------------------------------------------


def test_html_parser_extracts_visible_text_and_strips_script_style():
    html = (
        "<html><head><style>.a{color:red}</style>"
        "<script>evil()</script></head>"
        "<body><p>Visible One</p><p>Visible Two</p></body></html>"
    )
    document = HtmlParser().parse(ParseInput(content=html, filename="d.html"))

    assert "Visible One" in document.content
    assert "Visible Two" in document.content
    assert "evil" not in document.content
    assert "color:red" not in document.content
    # Provenance of the extraction backend is recorded honestly.
    assert document.metadata["text_engine"] == "beautifulsoup4/html.parser"
    assert [element.type for element in document.elements] == ["paragraph"]


# ---------------------------------------------------------------------------
# CsvParser
# ---------------------------------------------------------------------------


def test_csv_parser_renders_tab_separated_and_counts_rows():
    document = CsvParser().parse(
        ParseInput(content="name,role\nAda,engineer", filename="t.csv")
    )
    assert document.parser_name == "csv"
    assert document.content == "name\trole\nAda\tengineer"
    assert document.metadata["rows"] == 2
    assert [element.type for element in document.elements] == ["table"]
    assert document.elements[0].metadata["rows"] == 2


def test_csv_parser_sniffs_semicolon_dialect():
    document = CsvParser().parse(
        ParseInput(content="a;b;c\n1;2;3\n4;5;6", filename="t.csv")
    )
    # Sniffer detects ';' delimiter and re-emits as tab-separated.
    assert document.content == "a\tb\tc\n1\t2\t3\n4\t5\t6"


def test_csv_parser_handles_tsv_suffix():
    document = CsvParser().parse(ParseInput(content="a\tb\n1\t2", filename="t.tsv"))
    assert document.content == "a\tb\n1\t2"


# ---------------------------------------------------------------------------
# JsonParser
# ---------------------------------------------------------------------------


def test_json_parser_flattens_jsonpath_lines_and_keeps_raw_json_only():
    document = JsonParser().parse(
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
    document = JsonParser().parse(
        ParseInput(content='{"a": 1}\n\n{"b": 2}\n', filename="d.ndjson")
    )
    assert document.metadata["root_type"] == "list"
    assert "$[0].a: 1" in document.content
    assert "$[1].b: 2" in document.content


def test_json_flatten_caps_at_max_depth():
    nested: dict = {}
    cursor = nested
    for _ in range(JsonParser.MAX_FLATTEN_DEPTH + 50):
        cursor["a"] = {}
        cursor = cursor["a"]

    lines = JsonParser._flatten(nested)
    assert any(
        f"<max-depth {JsonParser.MAX_FLATTEN_DEPTH} reached>" in line for line in lines
    )


def test_json_flatten_renders_empty_containers():
    assert JsonParser._flatten({}) == ["$: {}"]
    assert JsonParser._flatten([]) == ["$: []"]


# ---------------------------------------------------------------------------
# Office parsers (docx / pptx / xlsx) via real tiny builders
# ---------------------------------------------------------------------------


def test_docx_parser_extracts_text(docx_bytes):
    document = DocxParser().parse(
        ParseInput(content=docx_bytes, filename="report.docx")
    )
    assert document.parser_name == "docx"
    assert "Hello Harbor" in document.content
    assert document.metadata["filename"] == "report.docx"


def test_pptx_parser_extracts_slide_text_and_count():
    document = PptxParser().parse(
        ParseInput(content=build_pptx_bytes("Kickoff slide"), filename="deck.pptx")
    )
    assert "Kickoff slide" in document.content
    assert document.metadata["slide_count"] == 1
    assert document.elements[0].metadata["slide"] == 1


def test_excel_parser_extracts_sheet_text_and_names():
    xlsx = build_xlsx_bytes([["header"], ["value-one"], ["value-two"]])
    document = ExcelParser().parse(ParseInput(content=xlsx, filename="book.xlsx"))
    assert document.parser_name == "excel"
    assert "value-one" in document.content
    assert "value-two" in document.content
    assert document.metadata["sheets"] == ["Sheet1"]
    assert document.elements[0].metadata["sheet"] == "Sheet1"


def test_fixture_builders_preserve_explicit_empty_collections():
    import io
    import zipfile

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(build_xlsx_bytes([])), read_only=True)
    assert list(workbook.active.values) == []

    epub = build_epub_bytes([])
    with zipfile.ZipFile(io.BytesIO(epub)) as archive:
        assert not any(name.endswith(".xhtml") for name in archive.namelist())
        assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED


def test_epub_fixture_builder_escapes_xhtml_section_text():
    import io
    import zipfile

    epub = build_epub_bytes(["A & <B>"])
    with zipfile.ZipFile(io.BytesIO(epub)) as archive:
        chapter = archive.read("OEBPS/ch1.xhtml").decode("utf-8")

    assert "A &amp; &lt;B&gt;" in chapter


# The legacy .xls path is exercised only for routing; building a real binary
# .xls fixture is not worth it, so we assert it routes to the excel parser.
def test_excel_parser_advertises_legacy_xls_route():
    assert ExcelParser().can_parse(ParseInput(content=b"x", filename="legacy.xls"))


# ---------------------------------------------------------------------------
# EpubParser
# ---------------------------------------------------------------------------


def test_epub_parser_preserves_spine_section_order():
    epub = build_epub_bytes(["Alpha section", "Beta section", "Gamma section"])
    document = EpubParser().parse(ParseInput(content=epub, filename="b.epub"))

    assert document.content == "Alpha section\n\nBeta section\n\nGamma section"
    assert [element.metadata["order"] for element in document.elements] == [1, 2, 3]
    assert document.metadata["sections"] == 3
    assert document.warnings is None


def test_epub_parser_warns_on_missing_referenced_section():
    import io
    import zipfile

    original = build_epub_bytes(["One text", "Two text"])
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(original)) as source,
        zipfile.ZipFile(buffer, "w") as sink,
    ):
        for info in source.infolist():
            if info.filename == "OEBPS/ch2.xhtml":
                continue  # spine still references it -> should warn, not crash
            sink.writestr(info, source.read(info.filename))

    document = EpubParser().parse(
        ParseInput(content=buffer.getvalue(), filename="b.epub")
    )
    assert document.content == "One text"
    assert document.warnings is not None
    assert any("ch2.xhtml" in warning for warning in document.warnings)
    assert [element.metadata["order"] for element in document.elements] == [1]


# ---------------------------------------------------------------------------
# parse_metadata provenance (spread-first fix)
# ---------------------------------------------------------------------------


def test_parse_metadata_document_cannot_override_computed_provenance():
    metadata = parse_metadata(
        ParseInput(
            content="x",
            filename="trusted.txt",
            content_type="text/plain",
            metadata={
                "filename": "spoofed.exe",
                "content_type": "application/x-evil",
                "extra": "kept",
            },
        )
    )
    assert metadata["filename"] == "trusted.txt"
    assert metadata["content_type"] == "text/plain"
    assert metadata["extra"] == "kept"


def test_parse_metadata_drops_none_values_but_keeps_extra():
    metadata = parse_metadata(
        ParseInput(content="x", filename="a.txt"),
        computed="present",
        skipped=None,
    )
    assert metadata["computed"] == "present"
    assert "skipped" not in metadata
    # content_type is None here, so it must not leak into provenance.
    assert "content_type" not in metadata


# ---------------------------------------------------------------------------
# HarborParser engine routing
# ---------------------------------------------------------------------------


def test_generic_content_type_does_not_conflict_with_specific_suffix():
    # Object stores often mislabel .csv as text/plain; the suffix must win.
    registry = HarborParser()
    parser = registry.parser_for(
        ParseInput(content="a,b\n1,2", filename="doc.csv", content_type="text/plain")
    )
    assert parser is not None
    assert parser.name == "csv"

    document = registry.parse(
        ParseInput(content="a,b\n1,2", filename="doc.csv", content_type="text/plain")
    )
    assert document.parser_name == "csv"


def test_conflicting_specific_signals_raise_unsupported_format():
    registry = HarborParser()
    conflicting = ParseInput(
        content="a,b\n1,2",
        filename="doc.csv",  # -> csv parser
        content_type="text/html",  # -> html parser (specific, not generic)
    )
    with pytest.raises(UnsupportedFormatError, match="Conflicting parser routes"):
        registry.parse(conflicting)


def test_content_type_used_when_suffix_absent():
    registry = HarborParser()
    parser = registry.parser_for(
        ParseInput(content="a,b\n1,2", content_type="text/csv")
    )
    assert parser is not None and parser.name == "csv"


def test_unknown_route_returns_none_from_parser_for():
    registry = HarborParser()
    assert (
        registry.parser_for(
            ParseInput(content=b"\x00", filename="x.zzz", content_type="x/y")
        )
        is None
    )


def test_parse_many_skip_isolates_bad_item():
    registry = HarborParser()
    documents = registry.parse_many(
        [
            ParseInput(content="alpha", filename="a.txt"),
            ParseInput(content=b"not a zip", filename="broken.docx"),
            ParseInput(content="gamma", filename="c.txt"),
        ],
        on_error="skip",
    )
    assert [document.content for document in documents] == ["alpha", "gamma"]


def test_parse_many_raise_propagates_first_failure():
    registry = HarborParser()
    with pytest.raises(ParseError):
        registry.parse_many(
            [
                ParseInput(content="alpha", filename="a.txt"),
                ParseInput(content=b"not a zip", filename="broken.docx"),
            ],
            on_error="raise",
        )


def test_parse_many_rejects_unknown_policy():
    with pytest.raises(ValueError, match="Unknown on_error policy"):
        HarborParser().parse_many([], on_error="bogus")


# ---------------------------------------------------------------------------
# Shared utils
# ---------------------------------------------------------------------------


def test_guard_input_size_allows_under_cap_and_rejects_over_cap():
    assert guard_input_size(b"small", max_bytes=10) == b"small"
    with pytest.raises(ParseError, match="exceeds max_input_bytes"):
        guard_input_size(b"x" * 11, max_bytes=10)
    # The default cap constant is exposed for callers to reason about.
    assert DEFAULT_MAX_INPUT_BYTES == 512 * 1024 * 1024


def test_compact_text_preserves_paragraph_breaks():
    assert compact_text("  a  \n\n\n  b  \n") == "a\n\nb"
    assert compact_text("\n\n") == ""


def test_html_to_text_with_engine_reports_beautifulsoup_backend():
    text, engine = html_to_text_with_engine("<p>Hello <b>world</b></p>")
    assert text == "Hello\nworld"
    assert engine == "beautifulsoup4/html.parser"
