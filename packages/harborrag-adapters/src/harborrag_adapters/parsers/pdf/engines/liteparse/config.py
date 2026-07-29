"""LiteParse provider configuration."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class LiteParsePDFConfig:
    """Configuration for LlamaIndex LiteParse PDF extraction."""

    output_format: str = "markdown"
    image_mode: str = "placeholder"
    extract_links: bool = True
    ocr_enabled: bool = True
    ocr_language: str = "eng"
    ocr_server_url: str | None = None
    tessdata_path: Path | str | None = None
    max_pages: int | None = None
    target_pages: str | None = None
    dpi: int | float | None = None
    preserve_very_small_text: bool | None = None
    password: str | None = None
    quiet: bool | None = None
    num_workers: int | None = None
    input_mode: str = "path"
    include_raw: bool = False
    parser: Any | None = None
    extra_options: dict[str, Any] = field(default_factory=dict)


__all__ = ["LiteParsePDFConfig"]
