"""PaddleOCR provider configuration."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PaddleOCRPDFConfig:
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


__all__ = ["PaddleOCRPDFConfig"]
