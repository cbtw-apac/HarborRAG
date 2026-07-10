from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, ClassVar

from harborrag_core.domain.parser import ParseInput

from ..exceptions import ParseError
from .base import PdfBackend, PdfParseResult
from .utils import (
    content_element,
    content_from_any,
    materialized_pdf_path,
    raise_if_encrypted_pdf,
)


@dataclass(slots=True)
class DoclingBackendOptions:
    """Configuration surface for Docling conversion and OCR behavior."""

    do_ocr: bool = True
    ocr_engine: str = "auto"
    ocr_lang: tuple[str, ...] = ("en",)
    force_full_page_ocr: bool = False
    bitmap_area_threshold: float | None = None
    do_table_structure: bool = True
    table_do_cell_matching: bool = True
    accelerator_device: str = "auto"
    accelerator_threads: int | None = 4
    pdf_backend: str | None = None
    max_num_pages: int | None = None
    max_file_size: int | None = None
    page_range: tuple[int, int] | None = None
    raises_on_error: bool = True
    export_format: str = "markdown"
    strict_text: bool = False
    include_raw_document: bool = False
    pipeline_options: Any | None = None
    converter: Any | None = None
    extra_convert_options: dict[str, Any] = field(default_factory=dict)


class DoclingBackend(PdfBackend):
    """Layout-aware PDF backend powered by IBM Docling."""

    name: ClassVar[str] = "docling"

    _OCR_OPTIONS: ClassVar[dict[str, str]] = {
        "easyocr": "EasyOcrOptions",
        "tesseract": "TesseractOcrOptions",
        "tesseract_cli": "TesseractCliOcrOptions",
        "rapidocr": "RapidOcrOptions",
        "ocrmac": "OcrMacOptions",
    }
    _PDF_BACKENDS: ClassVar[dict[str, tuple[str, str]]] = {
        "pypdfium2": (
            "docling.backend.pypdfium2_backend",
            "PyPdfiumDocumentBackend",
        ),
        "dlparse_v1": (
            "docling.backend.docling_parse_backend",
            "DoclingParseDocumentBackend",
        ),
        "dlparse_v2": (
            "docling.backend.docling_parse_v2_backend",
            "DoclingParseV2DocumentBackend",
        ),
    }

    def __init__(
        self,
        options: DoclingBackendOptions | None = None,
        **overrides: Any,
    ) -> None:
        """Create a backend, accepting either an options object or overrides."""

        from .utils import merge_dataclass_options

        self.options = merge_dataclass_options(
            options,
            DoclingBackendOptions,
            overrides,
        )
        self._cached_converter: Any = None

    def parse(self, input: ParseInput) -> PdfParseResult:
        """Convert a PDF through Docling and export the document content."""

        converter = self._converter()

        with materialized_pdf_path(input) as path:
            raise_if_encrypted_pdf(path)
            try:
                result = converter.convert(path, **self._convert_kwargs())
            except Exception as exc:  # noqa: BLE001 - external parser boundary
                raise ParseError(f"Docling could not parse PDF: {exc}") from exc

        document = getattr(result, "document", result)
        content = self._export_document(document)
        return PdfParseResult(
            content=content,
            engine=self.name,
            elements=content_element(self.name, content),
            metadata=self._metadata(result),
            raw=self._raw(document),
        )

    def _converter(self) -> Any:
        """Build (once) or reuse a Docling DocumentConverter for PDF inputs.

        Constructing a ``DocumentConverter`` loads hundreds of MB of layout/OCR
        models. Building it per document would make ingestion at scale
        prohibitively slow and thrash GPU memory, so the converter is memoized
        on this long-lived backend instance.
        """

        if self.options.converter is not None:
            return self.options.converter
        if self._cached_converter is not None:
            return self._cached_converter

        try:
            from docling.datamodel.base_models import InputFormat
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise ImportError(
                "PDF parsing with Docling requires `docling`; install "
                "`harborrag-adapters[pdf]` or `pip install docling`."
            ) from exc

        pipeline_options = self.options.pipeline_options or self._pipeline_options()
        format_option = self._pdf_format_option(PdfFormatOption, pipeline_options)
        self._cached_converter = DocumentConverter(
            format_options={InputFormat.PDF: format_option}
        )
        return self._cached_converter

    def _pipeline_options(self) -> Any:
        """Build PdfPipelineOptions while tolerating Docling version differences."""

        try:
            from docling.datamodel.pipeline_options import PdfPipelineOptions
        except ImportError as exc:
            raise ImportError(
                "Docling PDF pipeline options are unavailable in this installation."
            ) from exc

        pipeline_options = PdfPipelineOptions()
        do_ocr = self.options.do_ocr and self.options.ocr_engine.lower() != "none"
        self._set_supported(pipeline_options, "do_ocr", do_ocr)
        self._set_supported(
            pipeline_options,
            "do_table_structure",
            self.options.do_table_structure,
        )

        table_options = getattr(pipeline_options, "table_structure_options", None)
        if table_options is not None:
            self._set_supported(
                table_options,
                "do_cell_matching",
                self.options.table_do_cell_matching,
            )

        ocr_options = self._ocr_options()
        if ocr_options is not None:
            self._set_supported(pipeline_options, "ocr_options", ocr_options)
        else:
            existing_ocr_options = getattr(pipeline_options, "ocr_options", None)
            if existing_ocr_options is not None:
                self._configure_ocr_options(existing_ocr_options)

        accelerator_options = self._accelerator_options()
        if accelerator_options is not None:
            self._set_supported(
                pipeline_options,
                "accelerator_options",
                accelerator_options,
            )

        return pipeline_options

    def _ocr_options(self) -> Any | None:
        """Create a concrete Docling OCR option object when requested."""

        engine = self.options.ocr_engine.lower().strip()
        if engine in {"", "auto", "none"}:
            return None

        class_name = self._OCR_OPTIONS.get(engine)
        if class_name is None:
            supported = ", ".join(sorted([*self._OCR_OPTIONS, "auto", "none"]))
            raise ValueError(f"Unsupported Docling OCR engine {engine!r}: {supported}")

        pipeline_options = importlib.import_module("docling.datamodel.pipeline_options")
        options_cls = getattr(pipeline_options, class_name, None)
        if options_cls is None:
            raise ImportError(
                f"Docling installation does not expose {class_name} for OCR."
            )

        options = options_cls()
        self._configure_ocr_options(options)
        return options

    def _configure_ocr_options(self, options: Any) -> None:
        """Apply language and full-page OCR options to a Docling OCR object."""

        if self.options.ocr_lang:
            languages = list(self.options.ocr_lang)
            self._set_supported(options, "lang", languages)
            self._set_supported(options, "languages", languages)
        self._set_supported(
            options,
            "force_full_page_ocr",
            self.options.force_full_page_ocr,
        )
        self._set_supported(
            options,
            "bitmap_area_threshold",
            self.options.bitmap_area_threshold,
        )

    def _accelerator_options(self) -> Any | None:
        """Create Docling accelerator options when the installed version supports it."""

        if self.options.accelerator_threads is None and not self.options.accelerator_device:
            return None

        try:
            from docling.datamodel.accelerator_options import (
                AcceleratorDevice,
                AcceleratorOptions,
            )
        except ImportError:
            return None

        kwargs: dict[str, Any] = {}
        if self.options.accelerator_threads is not None:
            kwargs["num_threads"] = self.options.accelerator_threads

        device = self._accelerator_device(AcceleratorDevice)
        if device is not None:
            kwargs["device"] = device

        try:
            return AcceleratorOptions(**kwargs)
        except TypeError:
            kwargs.pop("device", None)
            return AcceleratorOptions(**kwargs)

    def _accelerator_device(self, accelerator_device: Any) -> Any | None:
        """Resolve a configured accelerator string into Docling's device enum."""

        configured = self.options.accelerator_device.lower().strip()
        if not configured:
            return None

        enum_name = configured.upper()
        device = getattr(accelerator_device, enum_name, None)
        if device is not None:
            return device

        try:
            return accelerator_device(configured)
        except Exception:  # noqa: BLE001 - enum compatibility boundary
            return getattr(accelerator_device, "AUTO", None)

    def _pdf_format_option(self, pdf_format_option: Any, pipeline_options: Any) -> Any:
        """Create PdfFormatOption and fall back when backend selection is unsupported."""

        kwargs: dict[str, Any] = {"pipeline_options": pipeline_options}
        backend = self._pdf_backend_class()
        if backend is not None:
            kwargs["backend"] = backend

        try:
            return pdf_format_option(**kwargs)
        except TypeError:
            kwargs.pop("backend", None)
            return pdf_format_option(**kwargs)

    def _pdf_backend_class(self) -> Any | None:
        """Resolve a named Docling PDF backend class."""

        backend = (self.options.pdf_backend or "").lower().strip()
        if not backend:
            return None

        location = self._PDF_BACKENDS.get(backend)
        if location is None:
            supported = ", ".join(sorted(self._PDF_BACKENDS))
            raise ValueError(f"Unsupported Docling PDF backend {backend!r}: {supported}")

        module_name, class_name = location
        module = importlib.import_module(module_name)
        backend_cls = getattr(module, class_name, None)
        if backend_cls is None:
            raise ImportError(
                f"Docling installation does not expose {class_name}."
            )
        return backend_cls

    def _convert_kwargs(self) -> dict[str, Any]:
        """Build keyword arguments for DocumentConverter.convert."""

        kwargs: dict[str, Any] = {
            "raises_on_error": self.options.raises_on_error,
        }
        for name in ("max_num_pages", "max_file_size", "page_range"):
            value = getattr(self.options, name)
            if value is not None:
                kwargs[name] = value
        kwargs.update(self.options.extra_convert_options)
        return kwargs

    def _export_document(self, document: Any) -> str:
        """Export a Docling document using the configured output format."""

        export_format = self.options.export_format.lower().strip()
        if export_format == "doctags":
            return content_from_any(self._call_export(document, "export_to_doctags"))
        if export_format == "text":
            value = self._call_export(
                document,
                "export_to_markdown",
                strict_text=True,
            )
            if value is None:
                value = self._call_export(document, "export_to_text")
            return content_from_any(value)

        value = self._call_export(
            document,
            "export_to_markdown",
            strict_text=self.options.strict_text,
        )
        return content_from_any(value if value is not None else document)

    def _metadata(self, result: Any) -> dict[str, Any]:
        """Collect stable Docling metadata without exposing heavy document objects."""

        metadata: dict[str, Any] = {
            "source_engine": self.name,
            "docling_export_format": self.options.export_format,
            "docling_ocr_engine": self.options.ocr_engine,
            "docling_do_ocr": self.options.do_ocr,
            "docling_do_table_structure": self.options.do_table_structure,
        }

        status = getattr(result, "status", None)
        if status is not None:
            metadata["docling_status"] = getattr(status, "name", str(status))

        pages = getattr(result, "pages", None)
        if pages is not None:
            try:
                metadata["page_count"] = len(pages)
            except TypeError:
                metadata["page_count"] = None

        errors = getattr(result, "errors", None)
        if errors:
            metadata["docling_errors"] = [str(error) for error in errors]

        return metadata

    def _raw(self, document: Any) -> dict[str, Any] | None:
        """Return a raw Docling payload only when explicitly requested."""

        if not self.options.include_raw_document:
            return None

        for method_name in ("export_to_dict", "model_dump", "dict"):
            value = self._call_export(document, method_name)
            if value is not None:
                return {"docling_document": value}
        return {"docling_document": content_from_any(document)}

    @staticmethod
    def _call_export(document: Any, method_name: str, **kwargs: Any) -> Any:
        """Call an export method and retry without kwargs for older Docling APIs."""

        method = getattr(document, method_name, None)
        if not callable(method):
            return None

        try:
            return method(**kwargs)
        except TypeError:
            return method()

    @staticmethod
    def _set_supported(target: Any, name: str, value: Any) -> bool:
        """Set a config value only when the target object supports that field."""

        if value is None:
            return False

        fields = getattr(target, "model_fields", None) or getattr(
            target,
            "__fields__",
            None,
        )
        if not hasattr(target, name) and (not fields or name not in fields):
            return False

        try:
            setattr(target, name, value)
            return True
        except Exception:  # noqa: BLE001 - third-party config object boundary
            return False
