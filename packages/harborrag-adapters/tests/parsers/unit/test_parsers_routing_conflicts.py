"""White-box unit tests for HarborParser route-conflict resolution and parse_many."""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers import HarborParserFactory
from harborrag_adapters.parsers.errors import ParseError, UnsupportedFormatError
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_generic_content_type_does_not_conflict_with_specific_suffix():
    # Object stores often mislabel .csv as text/plain; the suffix must win.
    registry = HarborParserFactory().create_registry()
    parser = registry.parser_for(
        ParseInput(content="a,b\n1,2", filename="doc.csv", content_type="text/plain")
    )
    assert parser is not None
    assert parser.parser_name == "spreadsheet"

    document = registry.parse(
        ParseInput(content="a,b\n1,2", filename="doc.csv", content_type="text/plain")
    )
    assert document.parser_name == "csv"


def test_conflicting_specific_signals_raise_unsupported_format():
    registry = HarborParserFactory().create_registry()
    conflicting = ParseInput(
        content="a,b\n1,2",
        filename="doc.csv",  # -> csv parser
        content_type="text/html",  # -> html parser (specific, not generic)
    )
    with pytest.raises(UnsupportedFormatError, match="Conflicting parser-family routes"):
        registry.parse(conflicting)


def test_content_type_used_when_suffix_absent():
    registry = HarborParserFactory().create_registry()
    parser = registry.parser_for(ParseInput(content="a,b\n1,2", content_type="text/csv"))
    assert parser is not None and parser.parser_name == "spreadsheet"


def test_unknown_route_returns_none_from_parser_for():
    registry = HarborParserFactory().create_registry()
    assert (
        registry.parser_for(ParseInput(content=b"\x00", filename="x.zzz", content_type="x/y"))
        is None
    )


def test_parse_many_skip_isolates_bad_item():
    registry = HarborParserFactory().create_registry()
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
    registry = HarborParserFactory().create_registry()
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
        HarborParserFactory().create_registry().parse_many([], on_error="bogus")


@pytest.mark.parametrize("content_type", ["text/plain", "application/octet-stream", None])
def test_unsupported_suffix_is_not_routed_by_a_generic_content_type(content_type):
    # A .srt or .rtf upload reported as text/plain must not reach the text
    # parser: the unregistered suffix is the stronger signal, not the guess.
    registry = HarborParserFactory().create_registry()
    unsupported = ParseInput(content="hello", filename="notes.srt", content_type=content_type)

    assert registry.parser_for(unsupported) is None
    with pytest.raises(UnsupportedFormatError, match="suffix='.srt'"):
        registry.parse(unsupported)


@pytest.mark.parametrize(
    "filename",
    ["README", "Release v2.0 Notes", "Version 2.0", None],
)
def test_names_without_a_file_suffix_still_route_by_content_type(filename):
    # Page titles and extensionless files carry no real suffix, so their
    # content type remains the only routing signal.
    registry = HarborParserFactory().create_registry()
    parser = registry.parser_for(
        ParseInput(content="hello", filename=filename, content_type="text/plain")
    )
    assert parser is not None and parser.parser_name == "text"


@pytest.mark.parametrize(
    ("extension", "supported"),
    [("md", True), (".PDF", True), ("dot", True), (".pot", True), ("xyz", False), (".srt", False)],
)
def test_supports_extension_includes_content_type_routed_suffixes(extension, supported):
    registry = HarborParserFactory().create_registry()
    assert registry.supports_extension(extension) is supported
