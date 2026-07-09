"""Targeted unit tests for pure-Python branches that broad tests miss.

Focuses on the core ParseInput contract, parser utils fallbacks/guards, and the
PDF engine's byte-level helpers (driven with real one-page PDFs via PyMuPDF).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.domain.parser import ParseInput, ParsedDocument, ParserFormat


# --------------------------------------------------------------------------- #
# core ParseInput: encoding, coercion shapes, is_supported, repr.
# --------------------------------------------------------------------------- #
def test_read_text_prefers_bom_encodings() -> None:
    assert ParseInput(content="café".encode("utf-8-sig")).read_text() == "café"
    assert ParseInput(content="café".encode("utf-16")).read_text() == "café"


def test_read_text_plain_utf8_and_explicit_encoding() -> None:
    assert ParseInput(content=b"hello").read_text() == "hello"
    assert ParseInput(content="né".encode("latin-1")).read_text(encoding="latin-1") == "né"


def test_read_text_uses_confidence_detection_not_cp1252_mojibake() -> None:
    # A clean UTF-8 payload with multibyte chars must round-trip, not decode as
    # cp1252 garbage.
    text = "Grüße — こんにちは"
    assert ParseInput(content=text.encode("utf-8")).read_text() == text


def test_read_text_replaces_undecodable_bytes_as_last_resort() -> None:
    out = ParseInput(content=b"\xff\xfe\x00bad\x81").read_text()
    assert isinstance(out, str)


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


# --------------------------------------------------------------------------- #
# parsers/utils: HTML fallback, guards, wrap_parse_errors branches.
# --------------------------------------------------------------------------- #
def test_html_to_text_bytes_and_entities() -> None:
    from harborrag_adapters.parsers.utils import html_to_text

    out = html_to_text(b"<p>Hello&amp;bye</p><script>x()</script>")
    assert "Hello&bye" in out
    assert "x()" not in out


def test_fallback_html_parser_used_when_bs4_absent(monkeypatch) -> None:
    import builtins

    from harborrag_adapters.parsers import utils

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "bs4":
            raise ImportError("no bs4")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    text, engine = utils.html_to_text_with_engine(
        "<div>A</div><p>B</p><script>hide()</script>"
    )
    assert engine == "python/html.parser"
    assert "A" in text and "B" in text and "hide()" not in text


def test_wrap_parse_errors_passthrough_and_normalize() -> None:
    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.utils import wrap_parse_errors

    with pytest.raises(ParseError, match="already"):
        with wrap_parse_errors("eng"):
            raise ParseError("already")

    with pytest.raises(ParseError, match="eng failed"):
        with wrap_parse_errors("eng"):
            raise KeyError("boom")


def test_guard_input_size_ok_and_over() -> None:
    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.utils import guard_input_size

    assert guard_input_size(b"abc", max_bytes=10) == b"abc"
    with pytest.raises(ParseError, match="max_input_bytes"):
        guard_input_size(b"abcdef", max_bytes=3)


def test_open_guarded_zip_member_count_limit(monkeypatch) -> None:
    import io
    import zipfile

    from harborrag_adapters.parsers import utils
    from harborrag_adapters.parsers.exceptions import ParseError

    monkeypatch.setattr(utils, "MAX_ARCHIVE_MEMBERS", 2)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(3):
            archive.writestr(f"f{i}.txt", "hi")
    with pytest.raises(ParseError, match="members"):
        utils.open_guarded_zip(buffer.getvalue())


# --------------------------------------------------------------------------- #
# pdf_engine helpers driven with real PyMuPDF PDFs.
# --------------------------------------------------------------------------- #
fitz = pytest.importorskip("fitz")


def _one_page_pdf(text: str = "Hello PDF world this is a sentence") -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


def test_pymupdf_backend_extracts_text() -> None:
    from harborrag_adapters.parsers.pdf_engine.pymupdf import PyMuPdfBackend

    result = PyMuPdfBackend().parse(ParseInput(content=_one_page_pdf(), filename="d.pdf"))
    assert "Hello PDF" in result.content
    assert result.engine == "pymupdf"
    assert result.metadata["page_count"] == 1


def test_pymupdf_backend_rejects_encrypted_pdf() -> None:
    from harborrag_adapters.parsers.exceptions import EncryptedPdfError
    from harborrag_adapters.parsers.pdf_engine.pymupdf import PyMuPdfBackend

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret content here")
    encrypted = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(EncryptedPdfError):
        PyMuPdfBackend().parse(ParseInput(content=encrypted, filename="e.pdf"))


def test_pymupdf_backend_bad_pdf_raises_parse_error() -> None:
    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.pdf_engine.pymupdf import PyMuPdfBackend

    with pytest.raises(ParseError):
        PyMuPdfBackend().parse(ParseInput(content=b"not a pdf", filename="x.pdf"))


def test_pdf_parser_end_to_end_and_materialized_path_from_disk(tmp_path: Path) -> None:
    from harborrag_adapters.parsers.pdf_engine import PdfParser
    from harborrag_adapters.parsers.pdf_engine.utils import materialized_pdf_path

    pdf = tmp_path / "real.pdf"
    pdf.write_bytes(_one_page_pdf())
    # When ParseInput has a real path, materialized_pdf_path yields it directly.
    with materialized_pdf_path(ParseInput(path=pdf)) as path:
        assert path == pdf

    document = PdfParser().parse(ParseInput(path=pdf))
    assert "Hello PDF" in document.content
    assert document.metadata["pdf_engine"] == "pymupdf"


def test_content_from_any_variants() -> None:
    from harborrag_adapters.parsers.pdf_engine.utils import content_from_any

    assert content_from_any(None) == ""
    assert content_from_any("  hi  ") == "hi"
    assert content_from_any(b"bytes text") == "bytes text"
    assert "a" in content_from_any({"markdown": "a"})
    assert content_from_any(["x", "y"]) == "x\ny"

    class Exportable:
        def export_to_markdown(self) -> str:
            return "exported md"

    assert content_from_any(Exportable()) == "exported md"


def test_walk_text_no_duplicate_and_depth_guard() -> None:
    from harborrag_adapters.parsers.pdf_engine.utils import _walk_text

    assert list(_walk_text({"text": "A", "nested": {"text": "B"}})) == ["A", "B"]
    cyclic: dict = {}
    cyclic["self"] = cyclic
    assert list(_walk_text(cyclic)) == []
