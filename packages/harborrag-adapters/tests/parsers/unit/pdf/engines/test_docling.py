"""Unit tests for the Docling PDF provider engine."""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from harborrag_adapters.parsers.compat import DoclingBackend, DoclingBackendOptions
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit


class _AcceleratorDevice(StrEnum):
    AUTO = "auto"
    CPU = "cpu"
    CUDA = "cuda"
    MPS = "mps"
    XPU = "xpu"


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
        max_file_size=2048,
        force_full_page_ocr=True,
        extra_convert_options={"custom": "value"},
    )
    configured = DoclingBackend(options, strict_text=True)

    assert configured.options.strict_text is True
    assert configured.options.force_full_page_ocr is True
    assert configured._convert_kwargs() == {
        "raises_on_error": True,
        "max_file_size": 2048,
        "custom": "value",
    }


@pytest.mark.whitebox
def test_docling_backend_options_normalize_yaml_lists():
    options = DoclingBackendOptions(ocr_lang=["en"])  # type: ignore[arg-type]

    assert options.ocr_lang == ("en",)


@pytest.mark.whitebox
@pytest.mark.parametrize("device", ["auto", "cpu", "cuda", "mps", "xpu"])
def test_docling_backend_preserves_supported_accelerator_devices(device: str):
    backend = DoclingBackend(accelerator_device=device)

    assert backend._accelerator_device(_AcceleratorDevice).value == device


@pytest.mark.whitebox
def test_docling_backend_preserves_a_specific_cuda_device():
    backend = DoclingBackend(accelerator_device="CUDA:2")

    assert backend._accelerator_device(_AcceleratorDevice) == "cuda:2"


@pytest.mark.whitebox
def test_docling_backend_rejects_an_invalid_accelerator_device():
    backend = DoclingBackend(accelerator_device="gpu")

    with pytest.raises(ValueError, match="Unsupported Docling accelerator device"):
        backend._accelerator_device(_AcceleratorDevice)


@pytest.mark.whitebox
def test_docling_backend_uses_docling_to_resolve_auto_acceleration(monkeypatch):
    requested: list[str] = []

    def _resolve(device: str) -> str:
        requested.append(device)
        return "xpu"

    docling_module = ModuleType("docling")
    utils_module = ModuleType("docling.utils")
    accelerator_module = ModuleType("docling.utils.accelerator_utils")
    accelerator_module.__dict__["decide_device"] = _resolve
    utils_module.__dict__["accelerator_utils"] = accelerator_module
    docling_module.__dict__["utils"] = utils_module
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.utils", utils_module)
    monkeypatch.setitem(
        sys.modules,
        "docling.utils.accelerator_utils",
        accelerator_module,
    )

    assert DoclingBackend().resolved_accelerator_device() == "xpu"
    assert requested == ["auto"]


@pytest.mark.whitebox
def test_docling_backend_rejects_encrypted_pdf_without_invoking_converter():
    from harborrag_adapters.parsers.errors import EncryptedPdfError

    class _ExplodingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "Docling converter must not run on an encrypted PDF; the "
                "PyMuPDF pre-check should short-circuit first."
            )

    backend = DoclingBackend(DoclingBackendOptions(converter=_ExplodingConverter()))

    with pytest.raises(EncryptedPdfError):
        backend.parse_input(ParseInput(content=_encrypted_pdf_bytes(), filename="secret.pdf"))


@pytest.mark.whitebox
def test_docling_backend_does_not_build_converter_for_encrypted_pdf(monkeypatch):
    from harborrag_adapters.parsers.errors import EncryptedPdfError

    backend = DoclingBackend()
    build_calls: list[str] = []

    def _record_and_build() -> None:
        build_calls.append("built")

    monkeypatch.setattr(backend, "_converter", _record_and_build)

    with pytest.raises(EncryptedPdfError):
        backend.parse_input(ParseInput(content=_encrypted_pdf_bytes(), filename="secret.pdf"))

    # The converter build (hundreds of MB of layout/OCR models) must not run
    # before the cheap encryption pre-check has a chance to reject the file.
    assert build_calls == []


@pytest.mark.whitebox
def test_docling_backend_rejects_oversized_pdf_with_a_clear_reason_without_invoking_converter():
    """A max_file_size violation used to only surface via Docling's own
    ConversionError, wrapped as "Docling could not parse PDF: ..." and then
    wrapped again by the PDF router as "engine failed (...)" -- the real
    reason (a configured size policy, not a parse failure) survived but was
    buried three layers deep behind a generic "could not parse" framing.
    The pre-check must reject it directly, with the size and limit in the
    message, before Docling's converter ever runs."""
    from harborrag_adapters.parsers.errors import ParseError

    class _ExplodingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "Docling converter must not run on an oversized PDF; the "
                "max_file_size pre-check should short-circuit first."
            )

    backend = DoclingBackend(
        DoclingBackendOptions(max_file_size=1, converter=_ExplodingConverter())
    )
    pdf_bytes = b"%PDF-1.4\n" + b"padding" * 10

    with pytest.raises(
        ParseError,
        match=rf"{len(pdf_bytes)} bytes exceeds the configured max_file_size limit of 1 bytes",
    ):
        backend.parse_input(ParseInput(content=pdf_bytes, filename="huge.pdf"))


@pytest.mark.whitebox
def test_docling_backend_max_file_size_none_does_not_reject_any_size():
    class _StubConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(document=SimpleNamespace(), errors=[])

    backend = DoclingBackend(DoclingBackendOptions(max_file_size=None, converter=_StubConverter()))

    backend.parse_input(ParseInput(content=b"%PDF-1.4\n" + b"padding" * 10, filename="ok.pdf"))


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

    result = backend.parse_input(ParseInput(content=content, filename="doc.pdf"))

    assert result.warnings == ["page 3 failed to parse"]


@pytest.mark.whitebox
def test_docling_backend_pipeline_options_enable_image_generation_when_output_dir_set(
    tmp_path,
):
    pytest.importorskip("docling")
    backend = DoclingBackend(image_output_dir=tmp_path, images_scale=1.5)

    pipeline_options = backend._pipeline_options()

    assert pipeline_options.generate_page_images is True
    assert pipeline_options.generate_picture_images is True
    assert pipeline_options.generate_table_images is True
    assert pipeline_options.images_scale == 1.5


@pytest.mark.whitebox
def test_docling_backend_leaves_image_generation_off_by_default():
    pytest.importorskip("docling")
    backend = DoclingBackend()

    pipeline_options = backend._pipeline_options()

    assert pipeline_options.generate_page_images is False
    assert pipeline_options.generate_picture_images is False
    assert pipeline_options.generate_table_images is False


class _FakeImage:
    def __init__(self, label: str) -> None:
        self.label = label
        self.saved_to: Any = None

    def save(self, path: Any) -> None:
        self.saved_to = path
        Path(path).write_text(self.label)


class _FakeElement:
    def __init__(self, image: _FakeImage | None) -> None:
        self._image = image

    def get_image(self, document: Any) -> _FakeImage | None:
        return self._image


@pytest.mark.whitebox
def test_docling_backend_saves_page_picture_and_table_images(tmp_path):
    document = SimpleNamespace(
        pages={1: SimpleNamespace(image=SimpleNamespace(pil_image=_FakeImage("page-1")))},
        pictures=[_FakeElement(_FakeImage("picture-1"))],
        tables=[_FakeElement(_FakeImage("table-1"))],
    )
    backend = DoclingBackend(image_output_dir=tmp_path)

    saved = backend._save_images(document)

    assert len(saved) == 3
    names = {Path(path).name for path in saved}
    assert names == {"page-1.png", "picture-1.png", "table-1.png"}
    for path in saved:
        assert Path(path).exists()
        assert Path(path).parent.parent == tmp_path


@pytest.mark.whitebox
def test_docling_backend_skips_elements_with_no_renderable_image(tmp_path):
    document = SimpleNamespace(
        pages={1: SimpleNamespace(image=None)},
        pictures=[_FakeElement(None)],
        tables=[],
    )
    backend = DoclingBackend(image_output_dir=tmp_path)

    assert backend._save_images(document) == []


@pytest.mark.whitebox
def test_docling_backend_parse_populates_image_paths_metadata(tmp_path):
    document = SimpleNamespace(
        pages={},
        pictures=[_FakeElement(_FakeImage("picture-1"))],
        tables=[],
    )

    class _RecordingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(document=document)

    backend = DoclingBackend(
        DoclingBackendOptions(converter=_RecordingConverter(), image_output_dir=tmp_path)
    )
    import fitz

    plain_pdf = fitz.open()
    try:
        plain_pdf.new_page().insert_text((72, 72), "not secret")
        content = plain_pdf.tobytes()
    finally:
        plain_pdf.close()

    result = backend.parse_input(ParseInput(content=content, filename="plain.pdf"))

    assert len(result.metadata["docling_image_paths"]) == 1
    assert Path(result.metadata["docling_image_paths"][0]).name == "picture-1.png"
