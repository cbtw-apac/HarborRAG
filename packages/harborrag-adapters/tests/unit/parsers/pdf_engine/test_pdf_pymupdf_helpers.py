"""Unit tests for PyMuPDF PDF backend helpers."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _one_page_pdf(text: str = "Hello PDF world this is a sentence") -> bytes:
    doc = fitz.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        return doc.tobytes()
    finally:
        doc.close()


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
    try:
        doc.new_page().insert_text((72, 72), "secret content here")
        encrypted = doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="o",
            user_pw="u",
        )
    finally:
        doc.close()
    with pytest.raises(EncryptedPdfError):
        PyMuPdfBackend().parse(ParseInput(content=encrypted, filename="e.pdf"))


def test_pymupdf_backend_bad_pdf_raises_parse_error() -> None:
    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.pdf_engine.pymupdf import PyMuPdfBackend

    with pytest.raises(ParseError):
        PyMuPdfBackend().parse(ParseInput(content=b"not a pdf", filename="x.pdf"))


def test_pymupdf_backend_rejects_pdf_exceeding_max_pages() -> None:
    # A degenerate/malicious PDF with an enormous page count must be rejected
    # before per-page extraction runs, since this backend has no subprocess
    # timeout to bound the work otherwise.
    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.pdf_engine.pymupdf import (
        PyMuPdfBackend,
        PyMuPdfBackendOptions,
    )

    doc = fitz.open()
    try:
        for _ in range(3):
            doc.new_page()
        three_page_pdf = doc.tobytes()
    finally:
        doc.close()

    backend = PyMuPdfBackend(PyMuPdfBackendOptions(max_pages=2))
    with pytest.raises(ParseError, match="max_pages"):
        backend.parse(ParseInput(content=three_page_pdf, filename="many.pdf"))


def test_pymupdf_backend_allows_pdf_within_max_pages() -> None:
    from harborrag_adapters.parsers.pdf_engine.pymupdf import (
        PyMuPdfBackend,
        PyMuPdfBackendOptions,
    )

    backend = PyMuPdfBackend(PyMuPdfBackendOptions(max_pages=5))
    result = backend.parse(ParseInput(content=_one_page_pdf(), filename="d.pdf"))
    assert result.metadata["page_count"] == 1


def test_pymupdf_backend_default_max_pages_is_bounded() -> None:
    from harborrag_adapters.parsers.pdf_engine.pymupdf import PyMuPdfBackend

    assert PyMuPdfBackend().options.max_pages == 1000


def test_pdf_parser_end_to_end_and_materialized_path_from_disk(tmp_path: Path) -> None:
    from harborrag_adapters.parsers.pdf_engine import PdfParser
    from harborrag_adapters.parsers.pdf_engine.utils import materialized_pdf_path

    pdf = tmp_path / "real.pdf"
    pdf.write_bytes(_one_page_pdf())
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
