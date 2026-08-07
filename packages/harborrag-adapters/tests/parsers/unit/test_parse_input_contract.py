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
from harborrag_adapters.parsers.errors import TextDecodingError
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
    with pytest.raises(TextDecodingError):
        read_parse_input_text(ParseInput(content=b"\xff\xfe\x00bad\x81"))


def test_read_text_raises_typed_error_instead_of_mojibake_on_corrupted_ascii() -> None:
    # A handful of invalid UTF-8 bytes inside an otherwise-ASCII document must
    # raise, not get silently reinterpreted as an unrelated legacy codepage
    # (charset_normalizer commonly guesses cp1251/Cyrillic for this shape of
    # input, since single-byte codepages accept almost any byte value).
    data = b"Hello world, this is a normal ASCII document with one bad byte: \x81 right there."
    with pytest.raises(TextDecodingError):
        read_parse_input_text(ParseInput(content=data))


def test_read_text_still_detects_genuine_legacy_encoded_text() -> None:
    text = "Привет мир, это тестовый документ на русском языке"
    assert read_parse_input_text(ParseInput(content=text.encode("cp1251"))) == text


def test_read_text_still_detects_bom_less_multi_byte_encoding() -> None:
    # Multi-byte guesses (UTF-16/UTF-32/...) are trusted without the non-ASCII
    # density gate that single-byte codepage guesses get: their code units are
    # structured enough that a confident match on corrupted-but-mostly-ASCII
    # bytes practically never happens, so a mostly-ASCII BOM-less UTF-16
    # document (which decoded correctly before the cp1251 fix) must keep
    # working rather than being rejected by the new density gate.
    text = "This is a long plain English sentence with only one accented letter near the end: café"
    assert read_parse_input_text(ParseInput(content=text.encode("utf-16-le"))) == text


def test_read_text_prefers_cp1252_over_mistaken_cp1250_guess() -> None:
    # cp1250 (Central European) and cp1252 (Western European) map ASCII
    # identically and both accept almost any high byte, so charset_normalizer
    # frequently mis-guesses a genuine cp1252 document as cp1250 (tied
    # chaos=0.0) -- silently substituting the wrong accented characters
    # (e.g. "naïve" -> "naďve", "señor" -> "seńor") instead of decoding
    # correctly.
    text = "Résumé of José García: über naïve façade, señor."
    assert read_parse_input_text(ParseInput(content=text.encode("cp1252"))) == text


def test_read_text_does_not_override_genuine_cp1250_document() -> None:
    # The cp1252 re-check must not kick in for text that is actually Central
    # European: cp1252 scores strictly worse (higher chaos) than cp1250 for
    # genuine Polish/Czech text, so detection must still land on cp1250.
    polish = "Dziękuję za wiadomość, proszę o odpowiedź jak najszybciej. Łódź, Wrocław, Kraków."
    czech = "Děkuji za váš dopis, prosím o odpověď co nejdříve. Praha, Brno, Ostrava, Plzeň."
    assert read_parse_input_text(ParseInput(content=polish.encode("cp1250"))) == polish
    assert read_parse_input_text(ParseInput(content=czech.encode("cp1250"))) == czech


def test_read_text_cp1250_cp1252_fix_does_not_affect_other_legacy_encodings() -> None:
    # The cp1250/cp1252 re-check is scoped to that specific confusion pair; it
    # must never trigger for unrelated single-byte codepages, which a broader
    # "always prefer cp1252" heuristic would silently corrupt (verified: a
    # Lithuanian cp1257 sample tied with cp1252 on chaos and was wrongly
    # swapped under that broader approach).
    samples = {
        "cp1257": "Labas, šis yra testinis dokumentas lietuvių kalba. Ąžuolas, žąsis, čiuožėjas.",
        "cp1253": "Καλημέρα, αυτό είναι ένα δοκίμιο στα ελληνικά.",
        "cp1255": "שלום, זהו מסמך בדיקה בעברית לצורך אימות זיהוי הקידוד.",
        "cp1256": "مرحبا، هذه وثيقة اختبار باللغة العربية للتحقق من الترميز.",
        "cp1254": "Merhaba, bu Türkçe bir test belgesidir. Şeker, çiçek, güneş, öğretmen, ışık.",
    }
    for encoding, text in samples.items():
        assert read_parse_input_text(ParseInput(content=text.encode(encoding))) == text


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
