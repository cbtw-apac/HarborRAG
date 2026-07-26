"""Unit tests for the HarborParser registry: routing, logging, content_from_any."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import pytest

from harborrag_adapters.parsers import HarborParserFactory, HarborParserRegistry
from harborrag_adapters.parsers.common.family import HarborSingleEngineFamilyParser
from harborrag_adapters.parsers.common.resources import read_parse_input_text
from harborrag_adapters.parsers.compat import (
    PARSER_LOGGER_NAME,
    BaseParser,
    HtmlParser,
    UnsupportedFormatError,
    get_parser_logger,
    parser_log_extra,
)
from harborrag_adapters.parsers.markup.parser import HarborMarkupParser
from harborrag_adapters.parsers.pdf.normalization import content_from_any
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

pytestmark = pytest.mark.unit


class FakeParser(BaseParser[ParseInput, ParsedDocument]):
    parser_name: ClassVar[str] = "fake"
    parser_engine: ClassVar[str] = "test-engine"
    suffixes: ClassVar[frozenset[str]] = frozenset({"fake"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/x-fake"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        parse_input = self.coerce_input(input)
        content = read_parse_input_text(parse_input)
        return ParsedDocument(
            content=content,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            elements=[DocumentElement(id="fake:0", type="paragraph", content=content)],
            metadata=self.metadata_for(parse_input),
        )


class OtherFakeParser(FakeParser):
    parser_name: ClassVar[str] = "other_fake"
    content_types: ClassVar[frozenset[str]] = frozenset({"application/x-other-fake"})


class BuggyParser(FakeParser):
    parser_name: ClassVar[str] = "buggy"
    suffixes: ClassVar[frozenset[str]] = frozenset({"buggy"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/x-buggy"})

    def parse(self, parse_input: ParseInput) -> ParsedDocument:
        raise TypeError("implementation bug")


class FakeFamily(HarborSingleEngineFamilyParser):
    parser_name = "fake"

    def __init__(self, engine: FakeParser | None = None) -> None:
        super().__init__((engine or FakeParser(),))


class OtherFakeFamily(FakeFamily):
    parser_name = "other_fake"


def test_parser_package_smoke_imports_and_default_registry():
    parser = HarborParserFactory().create_registry()

    assert parser.create("markup").parser_name == "markup"
    assert parser.create("spreadsheet").parser_name == "spreadsheet"
    assert parser.create("pdf").parser_name == "pdf"
    assert parser.create("text").parser_name == "text"
    assert parser.parser_for(ParseInput(content="# Hi", filename="doc.md")).parser_name == "markup"
    assert parser.parser_for(ParseInput(content=b"%PDF", filename="doc.pdf")).parser_name == "pdf"
    assert get_parser_logger().name == PARSER_LOGGER_NAME
    assert get_parser_logger("Registry").name == ("harborrag.adapters.parsers.registry")
    assert any(isinstance(handler, logging.NullHandler) for handler in get_parser_logger().handlers)


@pytest.mark.whitebox
def test_registry_indexes_routes_and_rejects_duplicate_ownership():
    registry = HarborParserRegistry()
    fake = FakeParser()
    family = FakeFamily(fake)
    registry.register_family(family)

    assert registry._families["fake"]() is family
    assert registry._extensions[".fake"]() is family
    assert registry._mime_types["application/x-fake"]() is family

    with pytest.raises(ValueError, match="already registered"):
        registry.register_family(OtherFakeFamily(OtherFakeParser()))

    registry.unregister("fake")
    assert registry.parser_for(ParseInput(content="hello", filename="doc.fake")) is None


@pytest.mark.whitebox
def test_registry_rejects_conflicting_suffix_and_content_type_routes():
    registry = HarborParserRegistry()
    registry.register_family(FakeFamily())
    registry.register_family(HarborMarkupParser((HtmlParser(),)))
    ambiguous = ParseInput(
        content="<p>hello</p>",
        filename="doc.fake",
        content_type="text/html",
    )

    with pytest.raises(UnsupportedFormatError, match="Conflicting parser-family routes"):
        registry.parse(ambiguous)


def test_unexpected_parser_bug_is_not_normalized_or_skipped():
    registry = HarborParserRegistry()
    registry.register_family(FakeFamily(BuggyParser()))
    parse_input = ParseInput(content="data", filename="doc.buggy")

    with pytest.raises(TypeError, match="implementation bug"):
        registry.parse_many([parse_input], on_error="skip")


@pytest.mark.whitebox
def test_pdf_result_normalizer_keeps_ocr_text_without_scores():
    raw_ocr = [
        [
            ([[0, 0], [1, 0], [1, 1], [0, 1]], ("Hello", 0.99)),
            ([[0, 2], [1, 2], [1, 3], [0, 3]], ("World", 0.98)),
        ]
    ]

    assert content_from_any(raw_ocr) == "Hello\nWorld"


@pytest.mark.whitebox
def test_content_from_any_does_not_recurse_forever_on_self_reference():
    cyclic: dict[str, Any] = {"text": "leaf value"}
    cyclic["self"] = cyclic

    # Must not raise RecursionError; the cycle guard should short-circuit
    # once the object has already been visited.
    assert content_from_any(cyclic) == "leaf value"


@pytest.mark.whitebox
def test_content_from_any_bails_out_on_pathologically_deep_nesting():
    nested: Any = "leaf value"
    for _ in range(1000):
        nested = {"content": nested}

    # Must not raise RecursionError; deep nesting past the walk-depth cap
    # bails out cleanly instead of blowing the Python call stack.
    content_from_any(nested)


@pytest.mark.graybox
def test_parser_registry_logs_route_and_result(caplog):
    caplog.set_level(logging.DEBUG, logger=PARSER_LOGGER_NAME)
    registry = HarborParserRegistry()
    registry.register_family(FakeFamily())

    document = registry.parse(
        ParseInput(
            content="hello",
            filename="doc.fake",
            content_type="application/x-fake",
        )
    )

    assert document.content == "hello"
    started = next(record for record in caplog.records if record.msg.startswith("Parsing"))
    finished = next(record for record in caplog.records if record.msg.startswith("Parsed"))
    assert started.name == "harborrag.adapters.parsers.registry"
    assert started.getMessage() == ("Parsing doc.fake with fake via mime_type=application/x-fake")
    assert finished.getMessage() == ("Parsed doc.fake with parser fake content_chars=5 elements=1")


@pytest.mark.graybox
def test_parser_log_extra_uses_safe_metadata_only():
    parse_input = ParseInput(
        content="secret body",
        filename="doc.md",
        content_type="text/markdown",
    )

    extra = parser_log_extra(
        input=parse_input,
        parser_name="markdown",
        parser_engine="python/regex",
        content_chars=11,
    )

    assert extra == {
        "harbor_parser": "markdown",
        "harbor_parser_engine": "python/regex",
        "harbor_input": "doc.md",
        "harbor_suffix": ".md",
        "harbor_content_type": "text/markdown",
        "harbor_content_chars": 11,
    }
    assert "secret body" not in repr(extra)
