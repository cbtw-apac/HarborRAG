"""Unit tests for the core ParseInput and ParsedDocument contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.parsers.common.resources import (
    coerce_parse_input,
    parse_input_suffix,
    parse_input_supports,
    read_parse_input_bytes,
    read_parse_input_text,
)
from harborrag_core.domain.parser import ParsedDocument, ParseInput, ParserFormat

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_read_text_prefers_bom_encodings() -> None:
    assert read_parse_input_text(ParseInput(content="cafe".encode("utf-8-sig"))) == "cafe"
    assert read_parse_input_text(ParseInput(content="cafe".encode("utf-16"))) == "cafe"


def test_read_text_plain_utf8_and_explicit_encoding() -> None:
    assert read_parse_input_text(ParseInput(content=b"hello")) == "hello"
    assert (
        read_parse_input_text(ParseInput(content="ne".encode("latin-1")), encoding="latin-1")
        == "ne"
    )


def test_read_text_uses_confidence_detection_not_cp1252_mojibake() -> None:
    text = "Grüße — こんにちは"
    assert read_parse_input_text(ParseInput(content=text.encode("utf-8"))) == text


def test_read_text_raises_on_undecodable_bytes_instead_of_replacing() -> None:
    with pytest.raises(UnicodeDecodeError):
        read_parse_input_text(ParseInput(content=b"\xff\xfe\x00bad\x81"))


def test_read_text_raises_instead_of_cp1251_mojibake_on_longer_invalid_utf8() -> None:
    """A short invalid byte string can fail statistical detection outright and
    mask this bug. Enough surrounding plain-ASCII text gives a single-byte
    detector (e.g. cp1251) real "confidence", so it used to return mis-decoded
    Cyrillic-looking text instead of raising -- confirm it raises instead."""
    data = (
        b"Hello world, this is a report.\xff\xfe\x00 more text after invalid "
        b"bytes here to pad length quite a bit so statistics work."
    )
    with pytest.raises(UnicodeDecodeError):
        read_parse_input_text(ParseInput(content=data))


def test_coerce_shapes() -> None:
    pi = ParseInput(content="x")
    assert coerce_parse_input(pi) is pi
    assert coerce_parse_input(b"bytes").content == b"bytes"
    assert coerce_parse_input("plain string").content == "plain string"

    class Rawish:
        source = "docs/report.pdf"
        content_type = "application/pdf"
        metadata = {"k": "v"}

        def text(self) -> str:
            return "body"

    coerced = coerce_parse_input(Rawish())
    assert coerced.content == "body"
    assert coerced.filename == "report.pdf"
    assert coerced.metadata == {"k": "v"}


def test_coerce_path_object_reads_from_disk(tmp_path: Path) -> None:
    target = tmp_path / "d.txt"
    target.write_text("disk body", encoding="utf-8")
    coerced = coerce_parse_input(target)
    assert coerced.path == target
    assert read_parse_input_text(coerced) == "disk body"


def test_parse_input_requires_path_or_content() -> None:
    with pytest.raises(ValueError, match="requires either"):
        ParseInput()


def test_suffix_and_repr() -> None:
    assert parse_input_suffix(ParseInput(content="x", filename="A.PDF")) == ".pdf"
    assert parse_input_suffix(ParseInput(content="x", path=Path("/tmp/z.MD"))) == ".md"
    assert "content_type" in repr(ParseInput(content="x", filename="a.txt"))
    assert "path=" in repr(ParseInput(content=b"x", path=Path("/tmp/a.txt")))


def test_is_supported_matches_suffix_and_content_type() -> None:
    pi = ParseInput(content="x", filename="a.json", content_type="application/json")
    assert parse_input_supports(pi, [ParserFormat.JSON])
    assert parse_input_supports(pi, ["json"])
    assert not parse_input_supports(pi, [ParserFormat.PDF])


def test_read_bytes_from_str_and_missing() -> None:
    assert read_parse_input_bytes(ParseInput(content="abc")) == b"abc"


def test_parsed_document_defaults() -> None:
    doc = ParsedDocument(content="c", parser_name="p")
    assert doc.parser_version == "1.0.0"
    assert doc.warnings is None and doc.raw is None
