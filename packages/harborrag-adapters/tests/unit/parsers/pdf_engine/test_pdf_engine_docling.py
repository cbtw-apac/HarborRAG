"""Unit tests for the Docling PDF backend."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from harborrag_adapters.parsers import DoclingBackend, DoclingBackendOptions
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit


def _encrypted_pdf_bytes() -> bytes:
    import fitz

    doc = fitz.open()
    try:
        doc.new_page().insert_text((72, 72), "secret content here")
        return doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="o",
            user_pw="u",
        )
    finally:
        doc.close()


@pytest.mark.whitebox
def test_docling_backend_options_build_convert_kwargs_without_importing_docling():
    options = DoclingBackendOptions(
        max_num_pages=3,
        max_file_size=2048,
        page_range=(1, 2),
        force_full_page_ocr=True,
        extra_convert_options={"custom": "value"},
    )
    configured = DoclingBackend(options, strict_text=True)

    assert configured.options.strict_text is True
    assert configured.options.force_full_page_ocr is True
    assert configured._convert_kwargs() == {
        "raises_on_error": True,
        "max_num_pages": 3,
        "max_file_size": 2048,
        "page_range": (1, 2),
        "custom": "value",
    }


@pytest.mark.whitebox
def test_docling_backend_rejects_encrypted_pdf_without_invoking_converter():
    from harborrag_adapters.parsers.exceptions import EncryptedPdfError

    class _ExplodingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "Docling converter must not run on an encrypted PDF; the "
                "PyMuPDF pre-check should short-circuit first."
            )

    backend = DoclingBackend(DoclingBackendOptions(converter=_ExplodingConverter()))

    with pytest.raises(EncryptedPdfError):
        backend.parse(ParseInput(content=_encrypted_pdf_bytes(), filename="secret.pdf"))


@pytest.mark.whitebox
def test_docling_backend_does_not_build_converter_for_encrypted_pdf(monkeypatch):
    from harborrag_adapters.parsers.exceptions import EncryptedPdfError

    backend = DoclingBackend()
    build_calls: list[str] = []

    def _record_and_build() -> None:
        build_calls.append("built")

    monkeypatch.setattr(backend, "_converter", _record_and_build)

    with pytest.raises(EncryptedPdfError):
        backend.parse(ParseInput(content=_encrypted_pdf_bytes(), filename="secret.pdf"))

    # The converter build (hundreds of MB of layout/OCR models) must not run
    # before the cheap encryption pre-check has a chance to reject the file.
    assert build_calls == []


@pytest.mark.whitebox
def test_docling_backend_surfaces_partial_failures_as_warnings():
    class _PartialResultConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                document=SimpleNamespace(),
                errors=["page 3 failed to parse"],
            )

    backend = DoclingBackend(DoclingBackendOptions(converter=_PartialResultConverter()))
    import fitz

    plain_pdf = fitz.open()
    try:
        plain_pdf.new_page().insert_text((72, 72), "not secret")
        content = plain_pdf.tobytes()
    finally:
        plain_pdf.close()

    result = backend.parse(ParseInput(content=content, filename="doc.pdf"))

    assert result.warnings == ["page 3 failed to parse"]


@pytest.mark.whitebox
def test_docling_backend_pre_check_does_not_block_normal_pdfs():
    calls: list[str] = []

    class _RecordingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("converted")
            return SimpleNamespace(document=SimpleNamespace())

    backend = DoclingBackend(DoclingBackendOptions(converter=_RecordingConverter()))
    import fitz

    plain_pdf = fitz.open()
    try:
        plain_pdf.new_page().insert_text((72, 72), "not secret")
        content = plain_pdf.tobytes()
    finally:
        plain_pdf.close()

    backend.parse(ParseInput(content=content, filename="plain.pdf"))

    assert calls == ["converted"]
