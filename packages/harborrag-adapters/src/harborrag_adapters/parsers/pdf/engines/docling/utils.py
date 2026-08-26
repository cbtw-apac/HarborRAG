from __future__ import annotations

import importlib
from typing import Any, ClassVar


class DoclingConfigurationMixin:
    """Construct Docling converter, OCR, backend, and accelerator options."""

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
    _ACCELERATOR_DEVICES: ClassVar[tuple[str, ...]] = (
        "auto",
        "cpu",
        "cuda",
        "mps",
        "xpu",
    )
    options: Any
    _cached_converter: Any

    @staticmethod
    def _set_supported(target: Any, name: str, value: Any) -> bool:
        raise NotImplementedError

    def _converter(self) -> Any:
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
                "`harborrag-adapters[pdf-docling]` or `pip install docling`."
            ) from exc
        pipeline_options = self.options.pipeline_options or self._pipeline_options()
        format_option = self._pdf_format_option(PdfFormatOption, pipeline_options)
        self._cached_converter = DocumentConverter(format_options={InputFormat.PDF: format_option})
        return self._cached_converter

    def _pipeline_options(self) -> Any:
        try:
            from docling.datamodel.pipeline_options import PdfPipelineOptions
        except ImportError as exc:
            raise ImportError(
                "Docling PDF pipeline options are unavailable in this installation."
            ) from exc
        pipeline_options = PdfPipelineOptions()
        do_ocr = self.options.do_ocr and self.options.ocr_engine.lower() != "none"
        self._set_supported(pipeline_options, "do_ocr", do_ocr)
        self._set_supported(pipeline_options, "do_table_structure", self.options.do_table_structure)
        table_options = getattr(pipeline_options, "table_structure_options", None)
        if table_options is not None:
            self._set_supported(
                table_options, "do_cell_matching", self.options.table_do_cell_matching
            )
        self._apply_ocr_options(pipeline_options)
        accelerator_options = self._accelerator_options()
        if accelerator_options is not None:
            self._set_supported(pipeline_options, "accelerator_options", accelerator_options)
        if self.options.image_output_dir is not None:
            self._set_supported(pipeline_options, "images_scale", self.options.images_scale)
            for name in (
                "generate_page_images",
                "generate_picture_images",
                "generate_table_images",
            ):
                self._set_supported(pipeline_options, name, True)
        return pipeline_options

    def _apply_ocr_options(self, pipeline_options: Any) -> None:
        ocr_options = self._ocr_options()
        if ocr_options is not None:
            self._set_supported(pipeline_options, "ocr_options", ocr_options)
            return
        existing = getattr(pipeline_options, "ocr_options", None)
        if existing is not None:
            self._configure_ocr_options(existing)

    def _ocr_options(self) -> Any | None:
        engine = self.options.ocr_engine.lower().strip()
        if engine in {"", "auto", "none"}:
            return None
        class_name = self._OCR_OPTIONS.get(engine)
        if class_name is None:
            supported = ", ".join(sorted([*self._OCR_OPTIONS, "auto", "none"]))
            raise ValueError(f"Unsupported Docling OCR engine {engine!r}: {supported}")
        module = importlib.import_module("docling.datamodel.pipeline_options")
        options_cls = getattr(module, class_name, None)
        if options_cls is None:
            raise ImportError(f"Docling installation does not expose {class_name} for OCR.")
        options = options_cls()
        self._configure_ocr_options(options)
        return options

    def _configure_ocr_options(self, options: Any) -> None:
        if self.options.ocr_lang:
            languages = list(self.options.ocr_lang)
            self._set_supported(options, "lang", languages)
            self._set_supported(options, "languages", languages)
        self._set_supported(options, "force_full_page_ocr", self.options.force_full_page_ocr)
        self._set_supported(options, "bitmap_area_threshold", self.options.bitmap_area_threshold)

    def _accelerator_options(self) -> Any | None:
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
        configured = self._configured_accelerator_device()
        if not configured:
            return None
        if configured.startswith("cuda:"):
            return configured
        device = getattr(accelerator_device, configured.upper(), None)
        if device is not None:
            return device
        try:
            return accelerator_device(configured)
        except (TypeError, ValueError):
            return configured

    def resolved_accelerator_device(self) -> str:
        configured = self._configured_accelerator_device() or "auto"
        try:
            from docling.utils.accelerator_utils import decide_device
        except ImportError as exc:
            raise ImportError(
                "Docling accelerator detection requires `docling`; install "
                "`harborrag-adapters[pdf-docling]` or `pip install docling`."
            ) from exc
        return str(decide_device(configured))

    def _configured_accelerator_device(self) -> str:
        configured = str(self.options.accelerator_device).lower().strip()
        if not configured:
            return ""
        if configured.startswith("cuda:"):
            _, separator, index = configured.partition(":")
            if separator and index.isdecimal():
                return configured
        elif configured in self._ACCELERATOR_DEVICES:
            return configured
        supported = ", ".join([*self._ACCELERATOR_DEVICES, "cuda:N"])
        raise ValueError(f"Unsupported Docling accelerator device {configured!r}: {supported}")

    def _pdf_format_option(self, pdf_format_option: Any, pipeline_options: Any) -> Any:
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
        backend = (self.options.pdf_backend or "").lower().strip()
        if not backend:
            return None
        location = self._PDF_BACKENDS.get(backend)
        if location is None:
            supported = ", ".join(sorted(self._PDF_BACKENDS))
            raise ValueError(f"Unsupported Docling PDF backend {backend!r}: {supported}")
        module_name, class_name = location
        backend_cls = getattr(importlib.import_module(module_name), class_name, None)
        if backend_cls is None:
            raise ImportError(f"Docling installation does not expose {class_name}.")
        return backend_cls

    def _convert_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"raises_on_error": self.options.raises_on_error}
        if self.options.max_file_size is not None:
            kwargs["max_file_size"] = self.options.max_file_size
        kwargs.update(self.options.extra_convert_options)
        return kwargs
