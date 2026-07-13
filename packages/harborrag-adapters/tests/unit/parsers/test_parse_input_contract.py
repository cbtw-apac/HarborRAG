"""Unit tests for the core ParseInput and ParsedDocument contracts."""
from __future__ import annotations

from pathlib import Path

import pytest
from harborrag_core.domain.parser import ParsedDocument, ParseInput, ParserFormat

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_read_text_prefers_bom_encodings() -> None:
    assert ParseInput(content="cafe".encode("utf-8-sig")).read_text() == "cafe"
    assert ParseInput(content="cafe".encode("utf-16")).read_text() == "cafe"


def test_read_text_plain_utf8_and_explicit_encoding() -> None:
    assert ParseInput(content=b"hello").read_text() == "hello"
    assert ParseInput(content="ne".encode("latin-1")).read_text(encoding="latin-1") == "ne"


def test_read_text_uses_confidence_detection_not_cp1252_mojibake() -> None:
    text = "Grüße — こんにちは"
    assert ParseInput(content=text.encode("utf-8")).read_text() == text


def test_read_text_raises_on_undecodable_bytes_instead_of_replacing() -> None:
    with pytest.raises(UnicodeDecodeError):
        ParseInput(content=b"\xff\xfe\x00bad\x81").read_text()


def test_coerce_shapes() -> None:
    pi = ParseInput(content="x")
    assert ParseInput.coerce(pi) is pi
    assert ParseInput.coerce(b"bytes").content == b"bytes"
    assert ParseInput.coerce("plain string").content == "plain string"

    class Rawish:
        source = "docs/report.pdf"
        content_type = "application/pdf"
        metadata = {"k": "v"}

        def text(self) -> str:
            return "body"

    coerced = ParseInput.coerce(Rawish())
    assert coerced.content == "body"
    assert coerced.filename == "report.pdf"
    assert coerced.metadata == {"k": "v"}


def test_coerce_path_object_reads_from_disk(tmp_path: Path) -> None:
    target = tmp_path / "d.txt"
    target.write_text("disk body", encoding="utf-8")
    coerced = ParseInput.coerce(target)
    assert coerced.path == target
    assert coerced.read_text() == "disk body"


def test_parse_input_requires_path_or_content() -> None:
    with pytest.raises(ValueError, match="requires either"):
        ParseInput()


def test_suffix_and_repr() -> None:
    assert ParseInput(content="x", filename="A.PDF").suffix == ".pdf"
    assert ParseInput(content="x", path=Path("/tmp/z.MD")).suffix == ".md"
    assert "content_type" in repr(ParseInput(content="x", filename="a.txt"))
    assert "path=" in repr(ParseInput(content=b"x", path=Path("/tmp/a.txt")))


def test_is_supported_matches_suffix_and_content_type() -> None:
    pi = ParseInput(content="x", filename="a.json", content_type="application/json")
    assert pi.is_supported([ParserFormat.JSON])
    assert pi.is_supported(["json"])
    assert not pi.is_supported([ParserFormat.PDF])


def test_read_bytes_from_str_and_missing() -> None:
    assert ParseInput(content="abc").read_bytes() == b"abc"


def test_parsed_document_defaults() -> None:
    doc = ParsedDocument(content="c", parser_name="p")
    assert doc.parser_version == "1.0.0"
    assert doc.warnings is None and doc.raw is None
