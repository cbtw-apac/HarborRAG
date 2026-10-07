"""Unit tests for the legacy Word 97-2003 binary (`.doc`) engine."""

from __future__ import annotations

import builtins

import pytest
from cfb_writer import build_compound_file
from harbor_test_builders import build_docx_bytes
from legacy_office_builders import build_doc_bytes, build_ppt_bytes

from harborrag_adapters.parsers.compat import MsWordParser
from harborrag_adapters.parsers.document.engines.msword import word97
from harborrag_adapters.parsers.document.engines.msword.text import strip_fields
from harborrag_adapters.parsers.errors import (
    ParseError,
    PasswordProtectedError,
    UnsupportedFormatError,
)
from harborrag_adapters.parsers.factory import HarborParserFactory
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.graybox]
pytest.importorskip("olefile", reason="legacy Office parser tests require the parsers extra")

VIETNAMESE = "Nguyễn Văn Á — Kỹ sư phần mềm"
CYRILLIC = "Иван Петров, инженер"


def _parse(data: bytes, filename: str = "cv.doc"):
    return MsWordParser().parse(ParseInput(content=data, filename=filename))


def test_utf16_pieces_preserve_vietnamese_and_cyrillic_text() -> None:
    document = _parse(build_doc_bytes(f"Curriculum Vitae\r{VIETNAMESE}\r{CYRILLIC}\r"))

    assert document.parser_name == "doc"
    assert document.content == f"Curriculum Vitae\n{VIETNAMESE}\n{CYRILLIC}"
    assert document.metadata["word_nfib"] == 0xC1
    assert [element.metadata["part"] for element in document.elements] == ["body"]


def test_compressed_cp1252_pieces_mix_with_utf16_pieces() -> None:
    text = f"Café résumé “quoted” — €5\r{VIETNAMESE}\r"
    document = _parse(build_doc_bytes(text, compress=True))

    assert document.content == f"Café résumé “quoted” — €5\n{VIETNAMESE}"


def test_field_instructions_are_stripped_but_results_kept() -> None:
    main = (
        'See \x13 HYPERLINK "https://example.test/cv" \x14my portfolio\x15 online.\r'
        "Page \x13 PAGE \x14\x13 NESTED \x147\x15\x15 end.\r"
        "\x13 TOC \\o \x15Visible\r"
    )
    document = _parse(build_doc_bytes(main))

    assert "See my portfolio online." in document.content
    assert "Page 7 end." in document.content
    assert "HYPERLINK" not in document.content
    assert "example.test" not in document.content
    assert "TOC" not in document.content
    assert "Visible" in document.content


def test_strip_fields_ignores_unbalanced_markers() -> None:
    assert strip_fields("a\x14b\x15c") == "abc"
    assert strip_fields("a\x13hidden") == "a"


def test_table_cells_become_a_table_element() -> None:
    main = "Experience\rCompany\x07Role\x07\x07ACME\x07Kỹ sư\x07\x07After the table\r"
    document = _parse(build_doc_bytes(main))

    kinds = [element.type for element in document.elements]
    assert kinds == ["paragraph", "table", "paragraph"]
    table = document.elements[1]
    assert table.content == "Company\tRole\nACME\tKỹ sư"
    assert table.metadata == {"part": "body", "rows": 2, "columns": 2}
    assert document.elements[2].content == "After the table"


def test_header_textbox_and_footnote_stories_are_extracted_separately() -> None:
    data = build_doc_bytes(
        "Body text\r",
        footnotes="\x02 Footnote text\r",
        headers="Confidential – Header\r\rFooter line\r",
        textboxes="Skills: Python, SQL\r",
        header_textboxes="Logo caption\r",
    )
    document = _parse(data)

    parts = {element.metadata["part"]: element.content for element in document.elements}
    assert parts["body"] == "Body text"
    assert parts["header"] == "Confidential – Header\n\nFooter line"
    assert parts["textbox"] == "Skills: Python, SQL"
    assert parts["header_textbox"] == "Logo caption"
    assert parts["footnote"] == "Footnote text"
    assert "Skills: Python, SQL" in document.content


def test_surrogate_pairs_keep_story_boundaries_aligned() -> None:
    document = _parse(build_doc_bytes("Hi 😀 there\r", headers="Header\r"))

    parts = {element.metadata["part"]: element.content for element in document.elements}
    assert parts == {"body": "Hi 😀 there", "header": "Header"}


def test_zero_table_stream_is_selected_by_fib_flag() -> None:
    document = _parse(build_doc_bytes("From 0Table\r", table_stream="0Table"))
    assert document.content == "From 0Table"


def test_encrypted_document_raises_password_protected() -> None:
    with pytest.raises(PasswordProtectedError):
        _parse(build_doc_bytes("secret\r", encrypted=True))


def test_encrypted_ooxml_package_named_doc_raises_password_protected() -> None:
    data = build_compound_file({"EncryptionInfo": b"\x04\x00", "EncryptedPackage": b"\x00"})
    with pytest.raises(PasswordProtectedError):
        _parse(data)


@pytest.mark.parametrize("ident", [0xA5EC, 0xA5DC, 0xA699])
def test_word6_and_word95_documents_are_unsupported(ident: int) -> None:
    with pytest.raises(UnsupportedFormatError, match="predates Word 97"):
        _parse(build_doc_bytes("old\r", n_fib=0x68, ident=ident))


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(build_doc_bytes("Some text\r")[:1536], id="truncated-ole"),
        pytest.param(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600, id="corrupt-header"),
        pytest.param(build_doc_bytes("x\r", table_stream="1Table")[:-4096], id="cut-stream"),
    ],
)
def test_corrupt_documents_raise_parse_error(data: bytes) -> None:
    with pytest.raises(ParseError):
        _parse(data)


def test_fib_and_piece_table_bounds_are_validated() -> None:
    with pytest.raises(ParseError, match="too short"):
        word97.read_fib(b"\xec\xa5" + b"\x00" * 10)
    with pytest.raises(ParseError, match="identifier"):
        word97.read_fib(b"\x00" * 64)
    fib = word97.WordFib(0xC1, "1Table", (("main", 10),), fc_clx=0, lcb_clx=500)
    with pytest.raises(ParseError, match="outside"):
        word97.read_pieces(b"\x02" + b"\x00" * 20, fib)
    piece = word97.Piece(cp_start=0, cp_end=50, byte_offset=0, compressed=False)
    with pytest.raises(ParseError, match="beyond"):
        word97.read_characters(b"\x00" * 10, [piece], 50)


def test_ole_file_without_word_stream_is_unsupported() -> None:
    with pytest.raises(UnsupportedFormatError, match="WordDocument"):
        _parse(build_ppt_bytes([]))


def test_renamed_docx_is_delegated_to_the_docx_engine() -> None:
    document = _parse(build_docx_bytes("Renamed package"), filename="cv.doc")
    assert document.parser_name == "docx"
    assert "Renamed package" in document.content


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        (b"{\\rtf1\\ansi Hello}", "RTF"),
        (b"<html><body>Hello</body></html>", "HTML"),
        (b"MIME-Version: 1.0\r\nContent-Type: multipart/related", "MHTML"),
        (b"plain bytes", "unrecognized"),
    ],
)
def test_non_binary_payloads_named_doc_are_unsupported(payload: bytes, label: str) -> None:
    with pytest.raises(UnsupportedFormatError, match=label):
        _parse(payload)


def test_empty_input_returns_empty_document() -> None:
    document = _parse(b"")
    assert document.content == ""
    assert document.elements == []


def test_missing_olefile_dependency_raises_parse_error(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "olefile":
            raise ImportError("no olefile")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ParseError, match="olefile"):
        _parse(build_doc_bytes("text\r"))


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [("CV.DOC", None), ("template.dot", "application/msword"), (None, "application/msword")],
)
def test_default_registry_routes_word_binary_to_document_family(filename, mime_type) -> None:
    registry = HarborParserFactory().create_registry()
    family = registry.resolve(filename=filename, mime_type=mime_type)
    assert family.name == "document"

    document = registry.parse(
        ParseInput(
            content=build_doc_bytes(f"{VIETNAMESE}\r"), filename=filename, content_type=mime_type
        )
    )
    assert VIETNAMESE in document.content


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [("graph.dot", "text/plain"), ("messages.pot", "text/plain")],
)
def test_text_files_sharing_an_office_template_suffix_stay_text(filename, mime_type) -> None:
    """A Graphviz graph or gettext template in a repository is not an Office file."""

    registry = HarborParserFactory().create_registry()

    document = registry.parse(
        ParseInput(content=b"digraph { a -> b }\n", filename=filename, content_type=mime_type)
    )

    assert "digraph" in document.content
