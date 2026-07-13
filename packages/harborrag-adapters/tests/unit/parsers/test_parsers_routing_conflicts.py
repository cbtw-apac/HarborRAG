"""White-box unit tests for HarborParser route-conflict resolution and parse_many."""

from __future__ import annotations

import pytest
from harborrag_adapters.parsers import HarborParser
from harborrag_adapters.parsers.exceptions import ParseError, UnsupportedFormatError
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


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
