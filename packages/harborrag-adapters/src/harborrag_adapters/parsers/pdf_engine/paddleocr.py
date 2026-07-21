from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from harborrag_core.domain.parser import ParseInput

from ..exceptions import ParseError
from .base import PdfBackend, PdfParseResult
from .utils import (
    content_element,
    content_from_any,
    materialized_pdf_path,
    merge_dataclass_options,
)


@dataclass(slots=True)
class PaddleOcrBackendOptions:
    """Configuration for PaddleOCR document parsing pipelines."""

    pipeline_class: str = "PPStructureV3"
    fallback_pipeline_classes: tuple[str, ...] = ("PaddleOCRVL", "PPStructure")
    lang: str | None = None
    device: str | None = None
    engine: str | None = None
    enable_hpi: bool | None = None
    use_tensorrt: bool | None = None
    precision: str | None = None
    enable_mkldnn: bool | None = None
    cpu_threads: int | None = None
    use_doc_orientation_classify: bool | None = None
    use_doc_unwarping: bool | None = None
    use_textline_orientation: bool | None = None
    use_table_recognition: bool | None = None
    use_formula_recognition: bool | None = None
    use_chart_recognition: bool | None = None
    use_region_detection: bool | None = None
    format_block_content: bool | None = None
    markdown_ignore_labels: tuple[str, ...] | None = None
    text_recognition_model_name: str | None = None
    text_detection_model_name: str | None = None
    layout_detection_model_name: str | None = None
    extra_options: dict[str, Any] = field(default_factory=dict)
    legacy_ocr_options: dict[str, Any] = field(default_factory=dict)


class PaddleOcrBackend(PdfBackend):
    """OCR-heavy PDF backend backed by PaddleOCR pipelines."""

    name: ClassVar[str] = "paddleocr"

    def __init__(
        self,
        options: PaddleOcrBackendOptions | None = None,
        **overrides: Any,
    ) -> None:
        """Create a PaddleOCR backend from options plus keyword overrides."""

        self.options = merge_dataclass_options(
            options,
            PaddleOcrBackendOptions,
            overrides,
        )
        self._active_pipeline_class: str | None = None
        self._cached_pipeline: Any = None
        self._cached_pipeline_class: str | None = None

    def parse(self, input: ParseInput) -> PdfParseResult:
        """Run the first supported PaddleOCR pipeline and normalize its output."""

        try:
            import paddleocr
        except ImportError as exc:
            raise ImportError(
                "PDF parsing with PaddleOCR requires `paddleocr`; install "
                "`harborrag-adapters[pdf]` or `pip install paddleocr`."
            ) from exc

        with materialized_pdf_path(input) as path:
            try:
                result, pipeline_warnings = self._predict(paddleocr, str(path))
            except Exception as exc:  # noqa: BLE001 - external parser boundary
                raise ParseError(f"PaddleOCR could not parse PDF: {exc}") from exc

        content = content_from_any(result)
        return PdfParseResult(
            content=content,
            engine=self.name,
            elements=content_element(self.name, content),
            metadata={
                "source_engine": self.name,
                "paddleocr_pipeline_class": self._active_pipeline_class,
                "paddleocr_device": self.options.device,
                "paddleocr_engine": self.options.engine,
            },
            warnings=pipeline_warnings,
        )

    def _predict(self, paddleocr: Any, path: str) -> tuple[Any, list[str]]:
        """Try modern document pipelines before falling back to legacy PaddleOCR.

        Falls back to the next configured pipeline class on ANY failure, not
        just when the class is missing from the ``paddleocr`` module.
        Construction (missing model weights, GPU init failure) and predict
        failures are the most common real-world failure mode and previously
        propagated straight out, aborting the whole fallback list instead of
        trying ``fallback_pipeline_classes``.
        """

        warnings: list[str] = []
        options = self._pipeline_options()
        class_names = (
            self.options.pipeline_class,
            *self.options.fallback_pipeline_classes,
        )
        for class_name in class_names:
            pipeline_cls = getattr(paddleocr, class_name, None)
            if pipeline_cls is None:
                continue
            try:
                # Reuse the already-loaded pipeline across documents;
                # constructing PPStructureV3/PaddleOCR loads large models and
                # is far too expensive to redo per document at ingestion scale.
                if self._cached_pipeline is not None and (
                    self._cached_pipeline_class == class_name
                ):
                    pipeline = self._cached_pipeline
                else:
                    pipeline = pipeline_cls(**options)
                    self._cached_pipeline = pipeline
                    self._cached_pipeline_class = class_name
                predict = getattr(pipeline, "predict", None)
                if callable(predict):
                    output = self._call_predict(predict, path)
                    markdown = self._concatenate_markdown(pipeline, output)
                    self._active_pipeline_class = class_name
                    return markdown or output, warnings
                if callable(pipeline):
                    self._active_pipeline_class = class_name
                    return pipeline(path), warnings
            except Exception as exc:  # noqa: BLE001 - fallback boundary, try next class
                warnings.append(f"{class_name}: {exc}")
                continue

        ocr_cls = getattr(paddleocr, "PaddleOCR", None)
        if ocr_cls is None:
            raise ImportError("PaddleOCR package does not expose a supported API.")

        self._active_pipeline_class = "PaddleOCR"
        ocr = ocr_cls(**self._legacy_ocr_options(options))
        predict = getattr(ocr, "predict", None)
        if callable(predict):
            return self._call_predict(predict, path), warnings
        ocr_method = getattr(ocr, "ocr", None)
        if callable(ocr_method):
            try:
                return ocr_method(path, cls=True), warnings
            except TypeError:
                return ocr_method(path), warnings

        raise ImportError("PaddleOCR package does not expose a supported parse method.")

    def _pipeline_options(self) -> dict[str, Any]:
        """Build PaddleOCR pipeline options, omitting unset values."""

        values = {
            "lang": self.options.lang,
            "device": self.options.device,
            "engine": self.options.engine,
            "enable_hpi": self.options.enable_hpi,
            "use_tensorrt": self.options.use_tensorrt,
            "precision": self.options.precision,
            "enable_mkldnn": self.options.enable_mkldnn,
            "cpu_threads": self.options.cpu_threads,
            "use_doc_orientation_classify": (self.options.use_doc_orientation_classify),
            "use_doc_unwarping": self.options.use_doc_unwarping,
            "use_textline_orientation": self.options.use_textline_orientation,
            "use_table_recognition": self.options.use_table_recognition,
            "use_formula_recognition": self.options.use_formula_recognition,
            "use_chart_recognition": self.options.use_chart_recognition,
            "use_region_detection": self.options.use_region_detection,
            "format_block_content": self.options.format_block_content,
            "markdown_ignore_labels": (
                list(self.options.markdown_ignore_labels)
                if self.options.markdown_ignore_labels is not None
                else None
            ),
            "text_recognition_model_name": (self.options.text_recognition_model_name),
            "text_detection_model_name": self.options.text_detection_model_name,
            "layout_detection_model_name": self.options.layout_detection_model_name,
        }
        return {
            key: value
            for key, value in {**values, **self.options.extra_options}.items()
            if value is not None
        }

    def _legacy_ocr_options(self, pipeline_options: dict[str, Any]) -> dict[str, Any]:
        """Translate modern options into the smaller legacy PaddleOCR surface."""

        if self.options.legacy_ocr_options:
            return dict(self.options.legacy_ocr_options)

        compatible_keys = {"lang", "use_angle_cls", "show_log"}
        return {key: value for key, value in pipeline_options.items() if key in compatible_keys}

    @staticmethod
    def _call_predict(predict: Any, path: str) -> Any:
        """Call predict across PaddleOCR APIs that disagree on argument names."""

        try:
            return predict(input=path)
        except TypeError:
            return predict(path)

    @staticmethod
    def _concatenate_markdown(pipeline: Any, output: Any) -> str:
        """Use PaddleOCR's page Markdown combiner when available."""

        concatenate = getattr(pipeline, "concatenate_markdown_pages", None)
        if not callable(concatenate):
            return ""

        markdown_pages = []
        for result in output or []:
            markdown = (
                result.get("markdown")
                if isinstance(result, dict)
                else getattr(result, "markdown", None)
            )
            if markdown:
                markdown_pages.append(markdown)
        if not markdown_pages:
            return ""
        return str(concatenate(markdown_pages))
