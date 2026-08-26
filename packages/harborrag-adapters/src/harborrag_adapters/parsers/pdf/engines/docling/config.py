"""Docling provider configuration."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class DoclingPDFConfig:
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
    max_file_size: int | None = None
    raises_on_error: bool = True
    export_format: str = "markdown"
    strict_text: bool = False
    include_raw_document: bool = False
    image_output_dir: Path | str | None = None
    images_scale: float = 2.0
    pipeline_options: Any | None = None
    converter: Any | None = None
    extra_convert_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize collection values produced by YAML configuration."""

        self.ocr_lang = tuple(self.ocr_lang)


DoclingBackendOptions = DoclingPDFConfig

__all__ = ["DoclingBackendOptions", "DoclingPDFConfig"]
